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


def _inbound_json(uuid: str, email: str) -> str:
    """JSON для `xray api adu` с одним пользователем."""
    client = {"id": uuid, "email": email}
    if config.VLESS_FLOW:
        client["flow"] = config.VLESS_FLOW
    return json.dumps(
        {
            "inbounds": [
                {
                    "tag": config.VLESS_INBOUND_TAG,
                    "listen": "0.0.0.0",
                    "port": config.VLESS_INBOUND_PORT,
                    "protocol": "vless",
                    "settings": {
                        "clients": [client],
                        "decryption": "none",
                    },
                }
            ]
        },
        ensure_ascii=False,
    )


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
    payload = base64.b64encode(_inbound_json(uuid, email).encode("utf-8")).decode("ascii")
    cmd = (
        f"echo {payload} | base64 -d > /tmp/vless-user.json && "
        f"{config.VLESS_XRAY_BIN} api adu --server={config.VLESS_API_SERVER} "
        f"/tmp/vless-user.json 2>&1; rm -f /tmp/vless-user.json"
    )
    try:
        rc, out, err = await _ssh(cmd)
    except Exception as e:
        log.warning("не удалось добавить VLESS-пользователя %s: %s", email, e)
        return False
    if "Added 1 user(s)" in out:
        log.info("VLESS-пользователь %s добавлен", email)
        return True
    # Уже существует — считаем успехом (идемпотентность).
    if "already exists" in out.lower() or "Added 0 user(s)" in out:
        log.info("VLESS-пользователь %s уже был на сервере", email)
        return True
    log.warning("xray api adu (%s): %s", email, (out or err)[:300])
    return False


async def remove_user(email: str) -> bool:
    """Удаляет пользователя с VLESS-сервера. True, если удалён или уже нет."""
    if not enabled():
        return False
    cmd = (
        f"{config.VLESS_XRAY_BIN} api rmu --server={config.VLESS_API_SERVER} "
        f"-tag={config.VLESS_INBOUND_TAG} '{email}' 2>&1"
    )
    try:
        rc, out, err = await _ssh(cmd)
    except Exception as e:
        log.warning("не удалось удалить VLESS-пользователя %s: %s", email, e)
        return False
    if "Removed 1 user(s)" in out:
        log.info("VLESS-пользователь %s удалён", email)
        return True
    if "not found" in out.lower():
        return True
    log.warning("xray api rmu (%s): %s", email, (out or err)[:300])
    return False
