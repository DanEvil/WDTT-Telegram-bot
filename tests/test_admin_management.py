"""Тесты динамических администраторов: назначение по TG ID и ссылкой-приглашением.

Админ-панель (сайт /admin, мини-апп вкладка 🛠, API /api/v1/admin/*) доступна
только тем, кто в конфиге ADMIN_IDS или назначен через панель (таблица admins).
"""
import pytest
from aiohttp import web

import api
import config
import db as dbmod
from config import ADMIN_IDS

ADMIN_TG = next(iter(ADMIN_IDS)) if ADMIN_IDS else 1035529538
NEW_ADMIN = 900000001
INVITED = 900000002


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


# ---------- db: админы и инвайты ----------

async def test_db_add_and_remove_admin():
    assert not dbmod.is_admin_cached(NEW_ADMIN)
    assert await dbmod.add_admin(NEW_ADMIN, added_by=ADMIN_TG, source="manual")
    assert dbmod.is_admin_cached(NEW_ADMIN)          # кеш обновился сразу
    assert not await dbmod.add_admin(NEW_ADMIN)       # повторно — False
    assert await dbmod.remove_admin(NEW_ADMIN)
    assert not dbmod.is_admin_cached(NEW_ADMIN)
    assert not await dbmod.remove_admin(NEW_ADMIN)    # уже снят


async def test_db_invite_lifecycle():
    inv = await dbmod.create_admin_invite(created_by=ADMIN_TG)
    assert inv["code"] and not inv["used"]
    assert await dbmod.consume_admin_invite(inv["code"], INVITED) == "ok"
    assert dbmod.is_admin_cached(INVITED)
    # одноразовая: второй раз — used
    assert await dbmod.consume_admin_invite(inv["code"], 900000003) == "used"
    assert not dbmod.is_admin_cached(900000003)
    await dbmod.remove_admin(INVITED)
    # отзыв неиспользованного
    inv2 = await dbmod.create_admin_invite(created_by=ADMIN_TG)
    assert await dbmod.revoke_admin_invite(inv2["code"])
    assert await dbmod.consume_admin_invite(inv2["code"], 900000004) == "not_found"
    assert not await dbmod.revoke_admin_invite("no-such-code")


# ---------- API ----------

async def test_admin_admins_forbidden_for_non_admin():
    import httpx
    runner, port = await _api_client()
    headers = {"Authorization": "Bearer " + _token(999999)}
    try:
        async with httpx.AsyncClient() as h:
            assert (await h.get(f"http://127.0.0.1:{port}/api/v1/admin/admins", headers=headers)).status_code == 403
            assert (await h.post(f"http://127.0.0.1:{port}/api/v1/admin/admins", headers=headers,
                                 json={"telegram_id": 1})).status_code == 403
            assert (await h.post(f"http://127.0.0.1:{port}/api/v1/admin/invites", headers=headers)).status_code == 403
    finally:
        await runner.cleanup()


async def test_admin_api_admins_flow():
    """Админ видит список (env-админы включены), назначает по ID, создаёт/отзывает инвайт."""
    import httpx
    await dbmod.remove_admin(NEW_ADMIN)   # чистое состояние
    await dbmod.remove_admin(INVITED)
    runner, port = await _api_client()
    headers = {"Authorization": "Bearer " + _token(ADMIN_TG)}
    try:
        async with httpx.AsyncClient() as h:
            # список: env-админ присутствует с source=env
            r = await h.get(f"http://127.0.0.1:{port}/api/v1/admin/admins", headers=headers)
            assert r.status_code == 200
            d = r.json()
            assert any(a["telegram_id"] == ADMIN_TG and a["source"] == "env" for a in d["admins"])
            assert "invites" in d

            # назначить по ID -> новый админ сразу видит админ-API
            r = await h.post(f"http://127.0.0.1:{port}/api/v1/admin/admins", headers=headers,
                             json={"telegram_id": NEW_ADMIN})
            assert r.status_code == 200
            new_headers = {"Authorization": "Bearer " + _token(NEW_ADMIN)}
            r = await h.get(f"http://127.0.0.1:{port}/api/v1/admin", headers=new_headers)
            assert r.status_code == 200, "назначенный админ должен видеть админ-панель"

            # невалидный ID -> 400
            r = await h.post(f"http://127.0.0.1:{port}/api/v1/admin/admins", headers=headers,
                             json={"telegram_id": -5})
            assert r.status_code == 400

            # нельзя снять env-админа и самого себя
            r = await h.post(f"http://127.0.0.1:{port}/api/v1/admin/admins/remove", headers=headers,
                             json={"telegram_id": ADMIN_TG})
            assert r.status_code == 400
            r = await h.post(f"http://127.0.0.1:{port}/api/v1/admin/admins/remove",
                             headers={"Authorization": "Bearer " + _token(NEW_ADMIN)},
                             json={"telegram_id": NEW_ADMIN})
            assert r.status_code == 400, "нельзя снять себя"

            # снять назначенного -> он теряет доступ
            r = await h.post(f"http://127.0.0.1:{port}/api/v1/admin/admins/remove", headers=headers,
                             json={"telegram_id": NEW_ADMIN})
            assert r.status_code == 200
            r = await h.get(f"http://127.0.0.1:{port}/api/v1/admin", headers=new_headers)
            assert r.status_code == 403, "снятый админ больше не видит админ-панель"

            # инвайт: создать -> погасить -> revoke
            r = await h.post(f"http://127.0.0.1:{port}/api/v1/admin/invites", headers=headers)
            assert r.status_code == 201
            inv = r.json()
            assert inv["ok"] and inv["code"]
            if inv["url"]:  # в тестах TELEGRAM_BOT_USERNAME может быть пустым
                assert inv["url"].endswith("start=admin_" + inv["code"])
            assert await dbmod.consume_admin_invite(inv["code"], INVITED) == "ok"
            r = await h.get(f"http://127.0.0.1:{port}/api/v1/admin",
                            headers={"Authorization": "Bearer " + _token(INVITED)})
            assert r.status_code == 200, "приглашённый по ссылке становится админом"
            # повторное использование невозможно
            assert await dbmod.consume_admin_invite(inv["code"], 900000005) == "used"

            r = await h.post(f"http://127.0.0.1:{port}/api/v1/admin/invites", headers=headers)
            code2 = r.json()["code"]
            r = await h.post(f"http://127.0.0.1:{port}/api/v1/admin/invites/revoke", headers=headers,
                             json={"code": code2})
            assert r.status_code == 200
            assert await dbmod.consume_admin_invite(code2, 900000006) == "not_found"
    finally:
        await dbmod.remove_admin(NEW_ADMIN)
        await dbmod.remove_admin(INVITED)
        await runner.cleanup()


async def test_is_admin_uid_helper():
    assert api.is_admin_uid(ADMIN_TG)
    assert not api.is_admin_uid(999999)
    await dbmod.add_admin(NEW_ADMIN, added_by=ADMIN_TG, source="manual")
    assert api.is_admin_uid(NEW_ADMIN)
    await dbmod.remove_admin(NEW_ADMIN)
    assert not api.is_admin_uid(NEW_ADMIN)
