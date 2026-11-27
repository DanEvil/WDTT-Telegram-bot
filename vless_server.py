"""Управление пользователями VLESS-сервера (xray) через его gRPC-API.

Схема работы: бот подключается к VLESS-серверу по SSH и вызывает штатные
команды xray:

    xray api adu --server=127.0.0.1:62790 <file.json>   # добавить пользователя
    xray api rmu --server=127.0.0.1:62790 -tag=vless-in <email>   # удалить

Пользователи добавляются и удаляются на лету — перезапуск xray не нужен,
активные сессии клиентов не рвутся.

Формат файла для `adu` — inbound-конфиг (как в /etc/xray-vless/config.json),
из которого xray берёт tag и список clients:

    {"inbounds": [{"tag": "vless-in", "listen": "0.0.0.0", "port": 443,
                   "protocol": "vless",
                   "settings": {"clients": [{"id": "<uuid>",
                                             "email": "<email>",
                                             "flow": "xtls-rprx-vision"}],
                                "decryption": "none"}}]}

Если VLESS_PER_USER выключен или не заданы SSH-доступы, модуль ничего не
делает и возвращает False — бот продолжает работать с общей ссылкой.
"""
import asyncio
import base64
import json
import logging

import config
from server_setup import ssh_run

log = logging.getLogger("vless_server")

# Метка пользователя в xray: однозначно идентифицирует клиента и служит
# ключом удаления (rmu работает по email).
EMAIL_PREFIX = "tg"


def email_for(telegram_id: int) -> str:
    """Метка пользователя xray для telegram_id."""
    return f"{EMAIL_PREFIX}{telegram_id}"


def enabled() -> bool:
    """Готова ли подсистема per-user UUID (включена и настроена)."""
    return bool(
        config.VLESS_PER_USER
        and config.VLESS_SSH_HOST
        and config.VLESS_SSH_PASSWORD
    )


async def _ssh(command: str, timeout: int = 30) -> tuple[int, str, str]:
    return await ssh_run(
        config.VLESS_SSH_HOST,
        config.VLESS_SSH_PORT,
        config.VLESS_SSH_USER,
        config.VLESS_SSH_PASSWORD,
        command,
        timeout=timeout,
    )


async def _ssh_retry(command: str, timeout: int = 30, attempts: int = 3) -> tuple[int, str, str]:
    """Выполняет команду с повторами: xray API иногда не отвечает с первого раза."""
    last = (1, "", "not executed")
    for i in range(attempts):
        try:
            last = await _ssh(command, timeout=timeout)
        except Exception as e:
            last = (1, "", str(e))
        if last[0] == 0 and "failed to dial" not in (last[1] + last[2]):
            return last
        if i < attempts - 1:
            await asyncio.sleep(1.5 * (i + 1))
    return last


def _client(uuid: str, email: str, with_flow: bool = True) -> dict:
    client = {"id": uuid, "email": email}
    # flow (xtls-rprx-vision) работает только в REALITY-инбаунде; XHTTP его не
    # поддерживает — для vless-xhttp-in клиент заводится без flow.
    if with_flow and config.VLESS_FLOW:
        client["flow"] = config.VLESS_FLOW
    return client


def _inbound_json(clients: list[dict], tag: str | None = None, port: int | None = None) -> str:
    """JSON для `xray api adu` со списком пользователей."""
    return json.dumps(
        {
            "inbounds": [
                {
                    "tag": tag or config.VLESS_INBOUND_TAG,
                    "listen": "0.0.0.0",
                    "port": port if port is not None else config.VLESS_INBOUND_PORT,
                    "protocol": "vless",
                    "settings": {
                        "clients": clients,
                        "decryption": "none",
                    },
                }
            ]
        },
        ensure_ascii=False,
    )


def _all_inbounds_json(clients: list[dict]) -> str:
    """JSON для adu со всеми инбаундами (REALITY + XHTTP, если XHTTP включён)."""
    inbounds = [
        {
            "tag": config.VLESS_INBOUND_TAG,
            "listen": "0.0.0.0",
            "port": config.VLESS_INBOUND_PORT,
            "protocol": "vless",
            "settings": {"clients": clients, "decryption": "none"},
        }
    ]
    if config.VLESS_XHTTP_ENABLED:
        inbounds.append(
            {
                "tag": config.VLESS_XHTTP_INBOUND_TAG,
                "listen": "0.0.0.0",
                "port": config.VLESS_XHTTP_INBOUND_PORT,
                "protocol": "vless",
                "settings": {
                    "clients": [{k: v for k, v in c.items() if k != "flow"} for c in clients],
                    "decryption": "none",
                },
            }
        )
    return json.dumps({"inbounds": inbounds}, ensure_ascii=False)


# ---------- файл со списком клиентов (переживает рестарт xray) ----------

async def read_clients() -> list[dict]:
    """Читает сохранённый список клиентов с VLESS-сервера."""
    if not enabled():
        return []
    path = config.VLESS_CLIENTS_FILE
    try:
        rc, out, err = await _ssh(f"cat {path} 2>/dev/null || echo '[]'")
    except Exception as e:
        log.warning("не удалось прочитать список VLESS-клиентов: %s", e)
        return []
    if rc != 0:
        return []
    try:
        data = json.loads(out.strip() or "[]")
    except ValueError:
        log.warning("файл клиентов VLESS повреждён: %s", out[:200])
        return []
    if isinstance(data, dict):
        data = data.get("clients", [])
    return [c for c in data if isinstance(c, dict) and c.get("id")]


async def write_clients(clients: list[dict]) -> bool:
    """Сохраняет список клиентов на VLESS-сервер (источник правды для sync-юнита)."""
    if not enabled():
        return False
    payload = base64.b64encode(json.dumps(clients, ensure_ascii=False).encode("utf-8")).decode("ascii")
    path = config.VLESS_CLIENTS_FILE
    cmd = (
        f"echo {payload} | base64 -d > {path}.tmp && "
        f"mv {path}.tmp {path} && chmod 600 {path} && echo OK"
    )
    try:
        rc, out, err = await _ssh(cmd)
    except Exception as e:
        log.warning("не удалось сохранить список VLESS-клиентов: %s", e)
        return False
    if rc != 0 or "OK" not in out:
        log.warning("сохранение списка VLESS-клиентов: %s", (err or out)[:200])
        return False
    return True


async def list_emails() -> list[str]:
    """Метки пользователей, заведённых на VLESS-сервере."""
    if not enabled():
        return []
    cmd = (
        f"{config.VLESS_XRAY_BIN} api inbounduser "
        f"--server={config.VLESS_API_SERVER} -tag={config.VLESS_INBOUND_TAG} 2>&1"
    )
    try:
        rc, out, err = await _ssh(cmd)
    except Exception as e:
        log.warning("не удалось получить список VLESS-пользователей: %s", e)
        return []
    if rc != 0:
        log.warning("xray api inbounduser вернул rc=%s: %s", rc, (err or out)[:200])
        return []
    try:
        data = json.loads(out)
    except ValueError:
        log.warning("xray api inbounduser вернул не-JSON: %s", out[:200])
        return []
    return [u.get("email", "") for u in data.get("users", []) if u.get("email")]


async def add_user(uuid: str, email: str) -> bool:
    """Добавляет пользователя на VLESS-сервер. True, если он там есть."""
    if not enabled():
        return False
    payload = base64.b64encode(_all_inbounds_json([_client(uuid, email)]).encode("utf-8")).decode("ascii")
    cmd = (
        f"echo {payload} | base64 -d > /tmp/vless-user.json && "
        f"{config.VLESS_XRAY_BIN} api adu --server={config.VLESS_API_SERVER} "
        f"/tmp/vless-user.json 2>&1; rm -f /tmp/vless-user.json"
    )
    try:
        rc, out, err = await _ssh_retry(cmd)
    except Exception as e:
        log.warning("не удалось добавить VLESS-пользователя %s: %s", email, e)
        return False
    if "Added 1 user(s)" in out:
        log.info("VLESS-пользователь %s добавлен", email)
    elif "already exists" in out.lower() or "Added 0 user(s)" in out:
        # Уже существует — считаем успехом (идемпотентность).
        log.info("VLESS-пользователь %s уже был на сервере", email)
    else:
        log.warning("xray api adu (%s): %s", email, (out or err)[:300])
        return False

    # Сохраняем список клиентов на диске: пользователи, добавленные через API,
    # живут только в памяти xray и исчезают при рестарте.
    clients = await read_clients()
    if not any(c.get("email") == email for c in clients):
        clients.append(_client(uuid, email))
        await write_clients(clients)
    return True


async def remove_user(email: str) -> bool:
    """Удаляет пользователя с VLESS-сервера. True, если удалён или уже нет."""
    if not enabled():
        return False
    cmd = (
        f"{config.VLESS_XRAY_BIN} api rmu --server={config.VLESS_API_SERVER} "
        f"-tag={config.VLESS_INBOUND_TAG} '{email}' 2>&1; "
        + (
            f"{config.VLESS_XRAY_BIN} api rmu --server={config.VLESS_API_SERVER} "
            f"-tag={config.VLESS_XHTTP_INBOUND_TAG} '{email}' 2>&1"
            if config.VLESS_XHTTP_ENABLED
            else "true"
        )
    )
    try:
        rc, out, err = await _ssh_retry(cmd)
    except Exception as e:
        log.warning("не удалось удалить VLESS-пользователя %s: %s", email, e)
        return False
    if "Removed 1 user(s)" not in out and "not found" not in out.lower():
        log.warning("xray api rmu (%s): %s", email, (out or err)[:300])
        return False

    clients = await read_clients()
    rest = [c for c in clients if c.get("email") != email]
    if len(rest) != len(clients):
        await write_clients(rest)

    log.info("VLESS-пользователь %s удалён", email)
    return True
