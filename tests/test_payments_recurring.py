"""Тесты: детект рекуррентов ЮKassa и очистка старых login_requests."""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import (  # noqa: E402
    cleanup_login_requests,
    create_login_request,
    get_login_request,
    confirm_login_request,
)
import payments  # noqa: E402


# ---------- детект рекуррентов ----------

class _Resp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeHTTP:
    """Подменяет HTTP-клиент ЮKassa."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, url, json=None, headers=None):
        self.calls.append((url, json))
        return self.responses.pop(0)


def _make_client(responses):
    """Фабрика async-клиента ЮKassa для monkeypatch."""
    async def _client():
        return _FakeHTTP(responses)
    return _client


def test_recurring_detected_as_unsupported(monkeypatch):
    """403 'can't make recurring payments' -> флаг False, платёж создаётся без карты."""
    monkeypatch.setattr(payments, "_recurring_supported", None)
    monkeypatch.setattr(payments, "get_http_client", _make_client([
        _Resp(403, {"description": "This store can't make recurring payments"}),
        _Resp(201, {"id": "p1", "status": "pending",
                    "confirmation": {"confirmation_url": "https://pay.example/p1"}}),
    ]))

    async def run():
        res = await payments.create_payment({"id": "month", "name": "Месячный", "price": 100, "days": 30}, 1)
        return res

    res = asyncio.run(run())
    assert res["url"] == "https://pay.example/p1"
    assert payments.recurring_supported() is False


def test_recurring_detected_as_supported(monkeypatch):
    """Успешное создание с save_payment_method -> флаг True."""
    monkeypatch.setattr(payments, "_recurring_supported", None)
    monkeypatch.setattr(payments, "get_http_client", _make_client([
        _Resp(201, {"id": "p2", "status": "pending",
                    "confirmation": {"confirmation_url": "https://pay.example/p2"}}),
    ]))

    async def run():
        return await payments.create_payment({"id": "month", "name": "Месячный", "price": 100, "days": 30}, 2)

    asyncio.run(run())
    assert payments.recurring_supported() is True


def test_unknown_state_is_none(monkeypatch):
    monkeypatch.setattr(payments, "_recurring_supported", None)
    assert payments.recurring_supported() is None


# ---------- очистка login_requests ----------

def test_cleanup_login_requests_removes_old_only(tmp_path, monkeypatch):
    import db as db_mod

    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)

    async def run():
        await db_mod.init_db()
        await create_login_request("old-code")
        await create_login_request("new-code")
        # подтверждаем и помечаем старый как использованный
        await confirm_login_request("old-code", 123)
        from db import mark_login_request_used
        await mark_login_request_used("old-code")
        await confirm_login_request("new-code", 456)

        # делаем old-code очень старым
        async with db_mod.conn() as db:
            await db.execute(
                "UPDATE login_requests SET created_at = ? WHERE code = ?",
                (int(__import__("time").time()) - 7200, "old-code"),
            )

        removed = await cleanup_login_requests(max_age=3600)
        remaining = {r["code"] for r in await _all_login_requests()}
        return removed, remaining

    async def _all_login_requests():
        async with db_mod.conn() as db:
            rows = await (await db.execute("SELECT code FROM login_requests")).fetchall()
            return [dict(r) for r in rows]

    removed, remaining = asyncio.run(run())
    assert removed >= 1
    assert "old-code" not in remaining
    assert "new-code" in remaining
