"""Регрессионные тесты блока стабильности/безопасности (D1–D6)."""
import asyncio
import pathlib

import pytest

import bot
import db
from wdtt import WdttError


# ---------- D1: upsert_customer не стирает метки напоминаний ----------

async def test_upsert_customer_keeps_flags_when_expiry_unchanged():
    tg = 9_000_001
    expires = 2_000_000_000
    await db.upsert_customer(tg, "pwd-1", expires, "month")

    assert await db.claim_reminder(tg, 3) is True
    assert await db.claim_traffic_notice(tg) is True

    # повторный upsert с тем же сроком (как делает sync_panel_worker)
    await db.upsert_customer(tg, "pwd-1", expires, "month")

    cust = await db.get_customer(tg)
    assert cust["reminder_sent"] == (1 << 3)
    assert cust["traffic_notified"] == 1


async def test_upsert_customer_resets_flags_on_new_expiry():
    tg = 9_000_002
    expires = 2_000_000_000
    await db.upsert_customer(tg, "pwd-2", expires, "month")
    await db.claim_reminder(tg, 3)
    await db.claim_traffic_notice(tg)

    # продление: срок изменился -> напоминания для нового периода сбрасываются
    await db.upsert_customer(tg, "pwd-2", expires + 30 * 86400, "month")

    cust = await db.get_customer(tg)
    assert cust["reminder_sent"] == 0
    assert cust["traffic_notified"] == 0


async def test_wal_mode_enabled():
    async with db.conn() as conn:
        row = await (await conn.execute("PRAGMA journal_mode")).fetchone()
    assert row[0].lower() == "wal"


# ---------- D2: sync_panel_worker обновляет только при смене срока ----------

def test_panel_expiry_changed_only_on_real_change():
    assert bot._panel_expiry_changed(100, 100) is False
    assert bot._panel_expiry_changed(100, 0) is False  # 0 = без срока, не трогаем
    assert bot._panel_expiry_changed(100, 200) is True
    assert bot._panel_expiry_changed(200, 100) is True


# ---------- D3: новый ключ только если пользователя нет в панели ----------

def test_panel_user_missing_classification():
    assert bot._panel_user_missing(WdttError("WDTT POST /panel/api/users/update: user not found"))
    assert not bot._panel_user_missing(WdttError("WDTT POST /panel/api/users/update -> HTTP 500: boom"))
    assert not bot._panel_user_missing(WdttError("WDTT users/update: timeout"))


def _customer(expires: int = 2_000_000_000) -> dict:
    return {
        "telegram_id": 9_000_010,
        "username": "u",
        "wdtt_password": "old-pwd",
        "expires_at": expires,
        "plan_id": "month",
        "server_id": 1,
        "sub_id": "sub-1",
    }


class _FakeClient:
    base_url = "https://panel.test"

    def __init__(self, update_error: Exception | None = None):
        self.update_error = update_error
        self.added = False

    async def get_user_info(self, pwd):
        return {"vk_hash": "", "expires_at": 2_000_000_000, "total_gb": 0, "max_devices": 1}

    async def update_user(self, **kwargs):
        if self.update_error:
            raise self.update_error

    async def reset_traffic(self, pwd):
        return None

    async def add_user(self, **kwargs):
        self.added = True
        return "new-pwd"


async def _run_grant(monkeypatch, update_error):
    client = _FakeClient(update_error)
    saved: list[tuple] = []

    async def fake_upsert(telegram_id, pwd, expires, plan_id, server_id=None, **kw):
        saved.append((telegram_id, pwd, expires))

    async def fake_ensure(*args, **kwargs):
        return None

    monkeypatch.setattr(bot, "get_customer", lambda tg: _as_coro(_customer()))
    monkeypatch.setattr(bot, "get_client", lambda sid: _as_coro(client))
    monkeypatch.setattr(bot, "upsert_customer", fake_upsert)
    monkeypatch.setattr(bot, "_ensure_vk_hash", fake_ensure)
    monkeypatch.setattr(bot, "_store_sub_mapping", fake_ensure)
    plan = {"id": "month", "days": 30, "traffic_gb": 0, "max_devices": 1}
    return client, saved, plan


async def _as_coro(value):
    return value


async def test_grant_access_does_not_duplicate_key_on_transient_error(monkeypatch):
    client, saved, plan = await _run_grant(
        monkeypatch, WdttError("WDTT POST /panel/api/users/update -> HTTP 500: boom")
    )
    with pytest.raises(WdttError):
        await bot.grant_access(9_000_010, "u", plan)
    assert client.added is False
    assert saved == []


async def test_grant_access_recreates_key_when_user_missing(monkeypatch):
    client, saved, plan = await _run_grant(
        monkeypatch, WdttError("WDTT POST /panel/api/users/update: user not found")
    )
    await bot.grant_access(9_000_010, "u", plan)
    assert client.added is True
    assert saved and saved[-1][1] == "new-pwd"


# ---------- D4: смена VK-cookies и вызов не должны пересекаться ----------

async def test_vk_cookie_rotation_is_serialized(monkeypatch):
    events: list[str] = []

    class Client:
        async def set_vk_cookies(self, cookies):
            events.append(f"cookies:{cookies}")
            await asyncio.sleep(0.01)

        async def create_vk_call(self, pwd):
            events.append("call")
            await asyncio.sleep(0.01)
            return "hash-1,hash-2"

    async def fake_pick(server_id):
        return {"id": 1, "label": "a", "cookies": "c=1"}

    async def fake_record(*args, **kwargs):
        return None

    monkeypatch.setattr(bot, "_pick_vk_account", fake_pick)
    monkeypatch.setattr(bot, "record_vk_issued", fake_record)

    client = Client()
    await asyncio.gather(
        bot._create_vk_hash_rotated(client, 1, "pwd-a"),
        bot._create_vk_hash_rotated(client, 1, "pwd-b"),
    )

    # без блокировки было бы cookies, cookies, call, call
    assert events == ["cookies:c=1", "call", "cookies:c=1", "call"]


# ---------- D5: /metrics не отдаётся всему интернету ----------

def test_metrics_requires_token_or_loopback():
    assert bot._metrics_allowed("127.0.0.1", None, "") is True
    assert bot._metrics_allowed("::1", None, "") is True
    assert bot._metrics_allowed("185.1.2.3", None, "") is False
    assert bot._metrics_allowed("185.1.2.3", "Bearer secret", "secret") is True
    assert bot._metrics_allowed("185.1.2.3", "Bearer wrong", "secret") is False
    assert bot._metrics_allowed("127.0.0.1", None, "secret") is False


# ---------- D6: данные с сервера не попадают в inline-JS ----------

def test_no_server_data_in_inline_js_handlers():
    js = (pathlib.Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "copyLink('${" not in js
    assert "pay('${" not in js
