"""Тесты авторизации и защиты эндпоинтов API."""
import hashlib
import hmac
import time
from urllib.parse import urlencode

import pytest
from aiohttp import web

import api
import config
import db


async def _register_payment(telegram_id: int, payment_id: str, amount: str = "249.00") -> None:
    await db.add_payment(telegram_id=telegram_id, username="@u", payment_id=payment_id,
                         plan_id="month", amount=amount)


async def _make_client() -> web.Application:
    app = web.Application()
    api.register_v1_routes(app)
    return app


def _token(uid: int) -> str:
    return api.make_session(uid, config.TELEGRAM_TOKEN or "test-token")


async def test_check_payment_requires_auth():
    """Без токена — 401."""
    app = await _make_client()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        import httpx
        async with httpx.AsyncClient() as h:
            r = await h.get(f"http://127.0.0.1:{port}/api/v1/check-payment?payment_id=abc")
            assert r.status_code == 401, r.status_code
    finally:
        await runner.cleanup()


async def test_check_payment_only_own_payment():
    """Чужой payment_id — 404 (нельзя узнать статус чужого платежа)."""
    await _register_payment(111, "pay-owner-1")
    app = await _make_client()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        import httpx
        async with httpx.AsyncClient() as h:
            r = await h.get(
                f"http://127.0.0.1:{port}/api/v1/check-payment?payment_id=pay-owner-1",
                headers={"Authorization": f"Bearer {_token(222)}"},
            )
            assert r.status_code == 404, (r.status_code, r.text)
    finally:
        await runner.cleanup()


async def test_session_signature_rejects_tampering():
    """Подделка подписи сессии — токен не принимается."""
    token = _token(111)
    body, sig = token.split(".", 1)
    bad = body + "." + ("a" * len(sig))
    assert api.verify_session(bad, config.TELEGRAM_TOKEN or "test-token") is None


async def test_session_expired_rejected():
    """Просроченная сессия не принимается."""
    expired = api._sign({"uid": 111, "exp": int(time.time()) - 10}, config.TELEGRAM_TOKEN or "test-token")
    assert api.verify_session(expired, config.TELEGRAM_TOKEN or "test-token") is None


async def test_valid_session_accepted():
    good = _token(111)
    assert api.verify_session(good, config.TELEGRAM_TOKEN or "test-token") == 111


async def test_init_data_rejects_missing_hash():
    """initData без hash — отказ."""
    assert api.validate_init_data("user=%7B%7D", "test-token") is None


async def test_init_data_rejects_bad_signature():
    """initData с неверной подписью — отказ."""
    assert api.validate_init_data("hash=deadbeef&user=%7B%22id%22%3A1%7D", "test-token") is None


def _make_init_data(fields: dict, token: str, signature: str | None = "sig") -> str:
    """Собирает initData так, как это делает Telegram (Bot API 8.0+)."""
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    params = {**fields, "hash": digest}
    if signature is not None:
        params["signature"] = signature
    return urlencode(params)


def test_init_data_with_signature_is_valid():
    """Поле signature (Bot API 8.0+) не должно ломать проверку hash."""
    token = "test-token"
    init_data = _make_init_data(
        {"auth_date": str(int(time.time())), "query_id": "AAH", "user": '{"id":42,"first_name":"T"}'},
        token,
    )
    user = api.validate_init_data(init_data, token)
    assert user is not None and user["id"] == 42


def test_init_data_tampered_user_rejected():
    """Подмена user при валидной подписи другого payload — отказ."""
    token = "test-token"
    init_data = _make_init_data(
        {"auth_date": str(int(time.time())), "user": '{"id":42}'},
        token,
    )
    assert "user=%7B%22id%22%3A42%7D" in init_data
    assert api.validate_init_data(init_data.replace("42", "43"), token) is None
