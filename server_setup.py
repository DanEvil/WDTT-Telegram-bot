"""Автоустановка панели WDTT на новый VPS по SSH + первоначальная настройка.

Сценарий (админ арендует сервер у провайдера и получает root-доступ):
1. Бот подключается по SSH (логин/пароль), скачивает deploy.sh из
   github.com/ildarmaga/wdtt и запускает его от root.
2. После установки панель доступна на http://<IP>:2860/wdtt/ (admin/wdtt).
3. Бот заходит в панель, меняет пароль админа, ставит лимит пользователей
   (SERVER_MAX_USERS = 100), загружает cookies первого VK-аккаунта.
4. Сервер сохраняется в БД (таблица servers) вместе с SSH-доступом.

paramiko — синхронная библиотека, поэтому все SSH-операции выполняются
в run_in_executor, чтобы не блокировать event loop бота.
"""
import asyncio
import logging
import re
import secrets
import string

import paramiko

import config
from wdtt import WdttClient

log = logging.getLogger("server_setup")


class SetupError(RuntimeError):
    """Ошибка установки/настройки сервера (текст показываем админу)."""


def _gen_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _gen_username() -> str:
    """Случайный логин панели вида adm_xxxxxxxx (вместо дефолтного admin)."""
    return "adm_" + secrets.token_hex(4)


def _ssh_run_sync(host: str, port: int, user: str, password: str,
                  command: str, timeout: int) -> tuple[int, str, str]:
    """Выполнить команду по SSH (синхронно). Возвращает (rc, stdout, stderr)."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            host, port=port, username=user, password=password,
            timeout=config.SSH_CONNECT_TIMEOUT, banner_timeout=config.SSH_CONNECT_TIMEOUT,
            auth_timeout=config.SSH_CONNECT_TIMEOUT,
            allow_agent=False, look_for_keys=False,
        )
        transport = client.get_transport()
        if transport is None:
            raise SetupError("SSH transport не открылся")
        chan = transport.open_session()
        chan.settimeout(timeout)
        chan.exec_command(command)
        out_parts, err_parts = [], []
        while True:
            if chan.recv_ready():
                out_parts.append(chan.recv(65536).decode("utf-8", "replace"))
            if chan.recv_stderr_ready():
                err_parts.append(chan.recv_stderr(65536).decode("utf-8", "replace"))
            if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                rc = chan.recv_exit_status()
                # дочитать остаток
                while chan.recv_ready():
                    out_parts.append(chan.recv(65536).decode("utf-8", "replace"))
                while chan.recv_stderr_ready():
                    err_parts.append(chan.recv_stderr(65536).decode("utf-8", "replace"))
                return rc, "".join(out_parts), "".join(err_parts)
            import time as _t
            _t.sleep(0.2)
    except Exception as e:
        raise SetupError(f"SSH {user}@{host}:{port}: {e}") from e
    finally:
        try:
            client.close()
        except Exception:
            pass


async def ssh_run(host: str, port: int, user: str, password: str,
                  command: str, timeout: int = 300) -> tuple[int, str, str]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _ssh_run_sync, host, port, user, password, command, timeout
    )


_PROGRESS_RE = re.compile(r"WDTT_PROGRESS\|([^|]*)\|?(.*)")


async def install_panel_over_ssh(host: str, port: int, user: str, password: str,
                                 main_password: str | None = None) -> list[str]:
    """Скачивает и запускает установщик панели на сервере. Возвращает строки прогресса."""
    main_password = main_password or _gen_password()
    # Экранируем для shell
    def q(s: str) -> str:
        return "'" + s.replace("'", "'\\''") + "'"

    install_cmd = (
        f"curl -fsSL {q(config.PANEL_INSTALLER_URL)} -o /tmp/wdtt-deploy.sh "
        f"&& WDTT_PASSWORD={q(main_password)} bash /tmp/wdtt-deploy.sh 2>&1"
    )
    # сначала проверим, что мы root (или есть sudo)
    rc, out, err = await ssh_run(host, port, user, password, "id -u", timeout=60)
    if rc != 0:
        raise SetupError(f"Не удалось выполнить id -u: {err or out}")
    if out.strip() != "0":
        rc2, out2, _ = await ssh_run(host, port, user, password, "sudo -n true 2>/dev/null && echo SUDO_OK", timeout=60)
        if "SUDO_OK" in out2:
            install_cmd = (
                f"curl -fsSL {q(config.PANEL_INSTALLER_URL)} -o /tmp/wdtt-deploy.sh "
                f"&& WDTT_PASSWORD={q(main_password)} sudo -n bash /tmp/wdtt-deploy.sh 2>&1"
            )
        else:
            raise SetupError("Нужен root или sudo без пароля на сервере")

    log.info("Установка панели на %s (это может занять несколько минут)...", host)
    rc, out, err = await ssh_run(host, port, user, password, install_cmd,
                                 timeout=config.PANEL_INSTALL_TIMEOUT)
    progress = [m.group(2) or m.group(1) for m in _PROGRESS_RE.finditer(out)]
    tail = "\n".join((out + err).strip().splitlines()[-25:])
    log.info("Установка на %s завершена rc=%s. Хвост вывода:\n%s", host, rc, tail)
    if rc != 0:
        raise SetupError(f"Установщик завершился с кодом {rc}. Хвост вывода:\n{tail}")
    return progress


async def configure_panel(host: str, panel_username: str | None = None,
                          new_panel_password: str | None = None,
                          vk_cookies: str | None = None) -> dict:
    """Первичная настройка свежей панели: смена логина+пароля, лимит 100, VK cookies.

    Возвращает {"base_url", "username", "password", "max_users"}.
    """
    base_url = f"http://{host}:2860/wdtt"
    new_panel_password = new_panel_password or _gen_password()
    new_panel_username = panel_username or _gen_username()
    client = WdttClient(base_url, "admin", "wdtt", verify_ssl=False)
    try:
        await client.login()
        # 1. смена логина и пароля панели (дефолт admin/wdtt -> сгенерированные)
        await client.set_panel_credentials(new_panel_password, new_panel_username)
        # перелогин с новыми данными
        await client.login()
        # 2. лимит пользователей на панель
        await client.set_max_users(config.SERVER_MAX_USERS)
        # 3. cookies VK (первый профиль; ротация — при создании хешей)
        cookies_ok = None
        if vk_cookies:
            st = await client.set_vk_cookies(vk_cookies)
            cookies_ok = bool(st.get("cookies_ok"))
        inbound = await client.get_inbound()
        return {
            "base_url": base_url,
            "username": new_panel_username,
            "password": new_panel_password,
            "max_users": int(inbound.get("max_users") or 0),
            "cookies_ok": cookies_ok,
        }
    except Exception as e:
        raise SetupError(f"Настройка панели на {host}: {e}") from e
    finally:
        await client.close()


async def deploy_server(host: str, ssh_port: int, ssh_user: str, ssh_password: str,
                        name: str, vk_cookies: str | None = None) -> dict:
    """Полный цикл: SSH-установка панели + настройка + запись в БД.

    Возвращает словарь созданного сервера (строка таблицы servers).
    """
    from db import add_server, list_vk_accounts  # локально, чтобы не тянуть db при импорте модуля

    await install_panel_over_ssh(host, ssh_port, ssh_user, ssh_password)

    if not vk_cookies:
        accounts = await list_vk_accounts()
        if accounts:
            vk_cookies = accounts[0]["cookies"]

    cfg = await configure_panel(host, vk_cookies=vk_cookies)
    server_id = await add_server(
        name=name,
        base_url=cfg["base_url"],
        username=cfg["username"],
        password=cfg["password"],
        verify_ssl=False,
        ssh_host=host,
        ssh_port=ssh_port,
        ssh_user=ssh_user,
        ssh_password=ssh_password,
    )
    log.info("Сервер %s (%s) установлен и добавлен как #%s", name, host, server_id)
    return {
        "id": server_id,
        "name": name,
        "host": host,
        "base_url": cfg["base_url"],
        "panel_username": cfg["username"],
        "panel_password": cfg["password"],
        "max_users": cfg["max_users"],
        "cookies_ok": cfg["cookies_ok"],
    }
