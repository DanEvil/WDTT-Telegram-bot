"""VK-хеши (звонки): по 4 каждому клиенту, добивка старым через админ-API, уведомление при отсутствии VK-аккаунтов."""
import asyncio

import pytest
from aiohttp import web

import api
import bot
import config
import web_app
from config import ADMIN_IDS
from wdtt import WdttClient

ADMIN_TG = next(iter(ADMIN_IDS)) if ADMIN_IDS else 1035529538


class FakePanelClient:
    """Фейк WdttClient: хранит vk_hash; update_user применяет его (как панель)."""

    def __init__(self, vk_hash=""):
        self.vk_hash = vk_hash
        self.update_calls = 0
        self.updated_vk_hash = None

    async def get_user_info(self, pwd):
        return {"vk_hash": self.vk_hash, "expires_at": 9999999999,
                "total_gb": 100, "max_devices": 2}

    async def update_user(self, *, old_password, password="", expires_at=0,
                          total_gb=0, max_devices=1, vk_hash=""):
        self.update_calls += 1
        self.updated_vk_hash = vk_hash
        self.vk_hash = vk_hash


def _patch_vk_rotation(monkeypatch, client):
    """Подменяет создание одного VK-вызова: панель применяет hash к пользователю."""
    state = {"n": 0}

    async def fake_create(cl, server_id, pwd):
        state["n"] += 1
        h = f"new{state['n']}"
        cl.vk_hash = (cl.vk_hash + "," + h) if cl.vk_hash else h
        return h

    monkeypatch.setattr(bot, "_create_vk_hash_rotated", fake_create)

    async def one_account(active_only=True):
        return [{"id": 1, "label": "vk1", "cookies": "remixsid=x"}]

    monkeypatch.setattr(bot, "list_vk_accounts", one_account)


# ---------- bot._ensure_vk_hash ----------

async def test_ensure_tops_up_to_four(monkeypatch):
    """Был 1 хеш — должно стать ровно VK_HASHES_PER_USER (4), старые не потеряны."""
    monkeypatch.setattr(config, "WDTT_VK_HASH", "")
    client = FakePanelClient(vk_hash="old1")
    _patch_vk_rotation(monkeypatch, client)
    await bot._ensure_vk_hash(client, "pwd", server_id=1)
    assert client.update_calls == 1
    pieces = WdttClient._clean_hash_pieces(client.updated_vk_hash)
    assert len(pieces) == config.VK_HASHES_PER_USER == 4
    assert pieces[0] == "old1"
    assert {"new1", "new2", "new3"} <= set(pieces)


async def test_ensure_noop_when_already_four(monkeypatch):
    monkeypatch.setattr(config, "WDTT_VK_HASH", "")
    client = FakePanelClient(vk_hash="a,b,c,d")
    _patch_vk_rotation(monkeypatch, client)
    await bot._ensure_vk_hash(client, "pwd", server_id=1)
    assert client.update_calls == 0


async def test_ensure_noop_with_shared_hash(monkeypatch):
    """WDTT_VK_HASH задан — общий хеш, создавать вызовы не нужно."""
    monkeypatch.setattr(config, "WDTT_VK_HASH", "shared_hash")
    client = FakePanelClient(vk_hash="")
    _patch_vk_rotation(monkeypatch, client)
    await bot._ensure_vk_hash(client, "pwd", server_id=1)
    assert client.update_calls == 0


async def test_notify_admin_once_when_no_vk_accounts(monkeypatch):
    """Нет VK-аккаунтов — админ уведомлён один раз за процесс, повторно молчит."""
    monkeypatch.setattr(config, "WDTT_VK_HASH", "")

    async def no_accounts(active_only=True):
        return []

    monkeypatch.setattr(bot, "list_vk_accounts", no_accounts)
    monkeypatch.setattr(bot, "_vk_no_accounts_notified", False)

    async def no_create(cl, server_id, pwd):
        return None

    monkeypatch.setattr(bot, "_create_vk_hash_rotated", no_create)

    sent = []

    async def fake_notify(text):
        sent.append(text)

    monkeypatch.setattr(bot, "notify_admin", fake_notify)

    await bot._ensure_vk_hash(FakePanelClient(vk_hash=""), "pwd", server_id=1)
    assert len(sent) == 1 and "VK" in sent[0]
    await bot._ensure_vk_hash(FakePanelClient(vk_hash=""), "pwd2", server_id=1)
    assert len(sent) == 1  # второй раз не спамим


# ---------- сайт: пробный период добивает 4 хеша общей функцией бота ----------

async def test_site_trial_issues_vk_hashes_via_bot_ensure(monkeypatch):
    seen = {}

    async def fake_ensure(client, pwd, server_id=None, target=None):
        seen["ensure"] = (pwd, server_id, target)

    monkeypatch.setattr(bot, "_ensure_vk_hash", fake_ensure)

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(web_app, "init_db", _noop)
    monkeypatch.setattr(web_app, "_require_site_session",
                        lambda req: {"site_user_id": 5, "telegram_id": 4242, "username": "u"})

    async def _claim(sub_id):
        return True

    monkeypatch.setattr(web_app, "claim_trial", _claim)

    async def _pick():
        return 7

    monkeypatch.setattr(web_app, "pick_least_loaded_server_id", _pick)

    async def _get_server(sid):
        return {"id": sid, "base_url": "https://panel.test:2860/wdtt",
                "username": "adm", "password": "p", "verify_ssl": False}

    monkeypatch.setattr(web_app, "get_server", _get_server)
    monkeypatch.setattr(web_app, "upsert_customer", _noop)

    class FakeWdtt:
        def __init__(self, *a, **kw):
            pass

        async def login(self):
            pass

        async def add_user(self, **kw):
            return "pw123"

        async def find_link(self, pwd):
            return "wdtt://Zm9v"

        async def close(self):
            pass

    monkeypatch.setattr(web_app, "WdttClient", FakeWdtt)

    class Req:
        match_info = {"plan_id": "trial"}

    resp = await web_app.handle_buy(Req())
    assert resp.status == 200
    assert seen.get("ensure") == ("pw123", 7, None)


# ---------- админ-API: POST /api/v1/admin/backfill-vk ----------

def _adm_token(uid: int) -> str:
    return api.make_session(uid, config.TELEGRAM_TOKEN or "test-token")


async def _api_client():
    app = web.Application()
    api.register_v1_routes(app)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, runner.addresses[0][1]


async def test_backfill_vk_requires_auth():
    import httpx
    runner, port = await _api_client()
    try:
        async with httpx.AsyncClient() as h:
            r = await h.post(f"http://127.0.0.1:{port}/api/v1/admin/backfill-vk")
            assert r.status_code == 401
    finally:
        await runner.cleanup()


async def test_backfill_vk_forbidden_for_non_admin():
    import httpx
    runner, port = await _api_client()
    headers = {"Authorization": "Bearer " + _adm_token(999999)}
    try:
        async with httpx.AsyncClient() as h:
            r = await h.post(f"http://127.0.0.1:{port}/api/v1/admin/backfill-vk", headers=headers)
            assert r.status_code == 403
    finally:
        await runner.cleanup()


async def test_backfill_vk_starts_background_for_admin(monkeypatch):
    """Админ получает 202 сразу; backfill_vk_hashes выполняется в фоне."""
    import httpx
    ran = {}

    async def fake_backfill(max_users=200):
        ran["done"] = True
        return {"checked": 3, "topped": 2, "failed": 0}

    monkeypatch.setattr(bot, "backfill_vk_hashes", fake_backfill)

    runner, port = await _api_client()
    headers = {"Authorization": "Bearer " + _adm_token(ADMIN_TG)}
    try:
        async with httpx.AsyncClient() as h:
            r = await h.post(f"http://127.0.0.1:{port}/api/v1/admin/backfill-vk", headers=headers)
            assert r.status_code == 202
            d = r.json()
            assert d["ok"] and d["started"] and d["target"] == config.VK_HASHES_PER_USER == 4
        for _ in range(50):
            await asyncio.sleep(0.05)
            if ran.get("done"):
                break
        assert ran.get("done"), "backfill_vk_hashes должен был запуститься в фоне"
    finally:
        await runner.cleanup()
