"""Тесты настройки ссылки PWDTT (Windows) из админ-панели: она попадает в подписку/инструкции/приложения."""
from aiohttp import web

import api
import config
import db as dbmod
import instructions as instr
from config import ADMIN_IDS

ADMIN_TG = next(iter(ADMIN_IDS)) if ADMIN_IDS else 1035529538
CUSTOM_URL = "https://example.com/pwdtt-latest.exe"


def _token(uid: int) -> str:
    return api.make_session(uid, config.TELEGRAM_TOKEN or "test-token")


async def _api_client():
    app = web.Application()
    api.register_v1_routes(app)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, runner.addresses[0][1]


async def _clear():
    async with dbmod.conn() as c:
        await c.execute("DELETE FROM settings WHERE key = 'app_windows_url'")
        await c.commit()


async def test_settings_db_roundtrip():
    await _clear()
    # по умолчанию — из конфига
    assert await dbmod.get_app_windows_url() == (config.APP_WINDOWS_URL or dbmod.DEFAULT_APP_WINDOWS_URL)
    await dbmod.set_setting("app_windows_url", CUSTOM_URL)
    assert await dbmod.get_setting("app_windows_url") == CUSTOM_URL
    assert await dbmod.get_app_windows_url() == CUSTOM_URL
    await _clear()
    assert await dbmod.get_setting("app_windows_url") is None
    assert await dbmod.get_app_windows_url() == (config.APP_WINDOWS_URL or dbmod.DEFAULT_APP_WINDOWS_URL)


async def test_platforms_with_windows_app_does_not_mutate():
    before = instr.PLATFORMS["windows"]["app_url"]
    plats = instr.platforms_with_windows_app(CUSTOM_URL)
    assert plats["windows"]["app_url"] == CUSTOM_URL
    assert instr.PLATFORMS["windows"]["app_url"] == before  # оригинал не тронут


async def test_admin_settings_api_flow():
    import httpx
    await _clear()
    runner, port = await _api_client()
    H = {"Authorization": "Bearer " + _token(ADMIN_TG)}
    Hbad = {"Authorization": "Bearer " + _token(999999)}
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient() as h:
            # не-админ — 403
            assert (await h.get(f"{base}/api/v1/admin/settings", headers=Hbad)).status_code == 403
            assert (await h.post(f"{base}/api/v1/admin/settings", headers=Hbad,
                                 json={"app_windows_url": CUSTOM_URL})).status_code == 403
            # админ читает текущую (дефолт)
            r = await h.get(f"{base}/api/v1/admin/settings", headers=H)
            assert r.status_code == 200
            assert r.json()["app_windows_url"] == (config.APP_WINDOWS_URL or dbmod.DEFAULT_APP_WINDOWS_URL)
            # битая ссылка — 400
            r = await h.post(f"{base}/api/v1/admin/settings", headers=H, json={"app_windows_url": "ftp://x y"})
            assert r.status_code == 400
            # валидная — сохраняется и попадает в ecosystem + instructions
            r = await h.post(f"{base}/api/v1/admin/settings", headers=H, json={"app_windows_url": CUSTOM_URL})
            assert r.status_code == 200 and r.json()["app_windows_url"] == CUSTOM_URL
            r = await h.get(f"{base}/api/v1/ecosystem")
            assert r.json()["apps"]["windows"] == CUSTOM_URL
            r = await h.get(f"{base}/api/v1/instructions")
            win = next(p for p in r.json()["platforms"] if p["key"] == "windows")
            assert win["app_url"] == CUSTOM_URL
            # пустое значение — сброс на дефолт
            r = await h.post(f"{base}/api/v1/admin/settings", headers=H, json={"app_windows_url": ""})
            assert r.status_code == 200 and r.json().get("reset")
            r = await h.get(f"{base}/api/v1/ecosystem")
            assert r.json()["apps"]["windows"] == (config.APP_WINDOWS_URL or dbmod.DEFAULT_APP_WINDOWS_URL)
    finally:
        await _clear()
        await runner.cleanup()
