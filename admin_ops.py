"""Админ-операции, общие для сайта и мини-аппа: фоновая установка панелей WDTT на новые сервера.

Состояние установок живёт здесь, чтобы и сайт (/admin), и API мини-аппа
(/api/v1/admin/*) видели одни и те же записи.
"""
import asyncio
import logging
import time

log = logging.getLogger("admin")

_installs: dict[int, dict] = {}
_install_seq = 0


async def _run_install(install_id: int, name: str, host: str, ssh_port: int, ssh_user: str, ssh_password: str) -> None:
    """Фоновая установка панели WDTT на новый сервер через SSH."""
    from server_setup import SetupError, deploy_server
    from db import list_vk_accounts

    rec = _installs[install_id]
    try:
        accounts = await list_vk_accounts()
        vk_cookies = accounts[0]["cookies"] if accounts else None
        result = await deploy_server(
            host=host, ssh_port=ssh_port, ssh_user=ssh_user,
            ssh_password=ssh_password, name=name, vk_cookies=vk_cookies,
        )
        rec.update(status="done", detail=(
            f"Сервер #{result['id']} добавлен: {result['base_url']} | "
            f"логин панели: {result['panel_username']} | пароль: {result['panel_password']}"
        ))
        log.info("admin: сервер %s (%s) установлен как #%s", name, host, result["id"])
    except SetupError as e:
        rec.update(status="error", detail=str(e)[:1500])
        log.warning("admin: установка %s (%s) не удалась: %s", name, host, e)
    except Exception as e:
        rec.update(status="error", detail=str(e)[:1500])
        log.exception("admin: установка %s (%s)", name, host)
    finally:
        rec["ssh_password"] = None  # пароль SSH больше не храним


def start_install(name: str, host: str, ssh_port: int, ssh_user: str, ssh_password: str) -> dict:
    """Создаёт запись об установке и запускает её фоном. Возвращает запись."""
    global _install_seq
    _install_seq += 1
    iid = _install_seq
    rec = {
        "id": iid, "name": name, "host": host, "status": "running",
        "detail": "", "started": int(time.time()), "ssh_password": None,
    }
    _installs[iid] = rec
    asyncio.create_task(_run_install(iid, name, host, ssh_port, ssh_user, ssh_password))
    return rec


def list_installs(limit: int = 10) -> list[dict]:
    return sorted(_installs.values(), key=lambda x: x["id"], reverse=True)[:limit]


def any_install_running() -> bool:
    return any(i["status"] == "running" for i in _installs.values())


def public_install(rec: dict) -> dict:
    """Запись без секретов — для API и страниц."""
    return {k: v for k, v in rec.items() if k != "ssh_password"}
