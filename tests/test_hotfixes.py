"""Регрессионные тесты hotfix-блока (C1–C5)."""
import time

import pytest

import bot
import web_app
from config import _duplicate_port_errors


# ---------- C1: /pay/success не «сжигает» платёж без выдачи ----------

def _request(payment_id: str):
    class Req:
        query = {"payment_id": payment_id}
    return Req()


def _session(telegram_id: int) -> dict:
    return {"site_user_id": 1, "telegram_id": telegram_id, "email": "u@example.com", "created": time.time()}


async def test_pay_success_foreign_payment_is_ignored(monkeypatch):
    """Чужой payment_id не должен запускать fulfillment."""
    calls: list[str] = []

    async def fake_fulfill(payment_id: str) -> None:
        calls.append(payment_id)

    async def fake_get_payment(payment_id: str):
        return {"payment_id": payment_id, "telegram_id": 222, "status": "paid"}

    monkeypatch.setattr(bot, "fulfill_payment", fake_fulfill)
    monkeypatch.setattr(web_app, "_get_session", lambda request: _session(111))
    monkeypatch.setattr(web_app, "get_payment", fake_get_payment)

    response = await web_app.handle_pay_success(_request("pay-foreign"))
    assert response.status == 200
    assert calls == []


async def test_pay_success_own_payment_grants_access(monkeypatch):
    """Свой платёж -> вызывается fulfill_payment, статус становится fulfilled."""
    calls: list[str] = []
    state = {"status": "paid"}

    async def fake_fulfill(payment_id: str) -> None:
        calls.append(payment_id)
        state["status"] = "fulfilled"

    async def fake_get_payment(payment_id: str):
        return {"payment_id": payment_id, "telegram_id": 111, "status": state["status"]}

    monkeypatch.setattr(bot, "fulfill_payment", fake_fulfill)
    monkeypatch.setattr(web_app, "_get_session", lambda request: _session(111))
    monkeypatch.setattr(web_app, "get_payment", fake_get_payment)

    response = await web_app.handle_pay_success(_request("pay-own"))
    assert response.status == 200
    assert calls == ["pay-own"]
    assert "активирована" in response.text


async def test_pay_success_without_session_does_not_fulfill(monkeypatch):
    """Без сессии нельзя инициировать выдачу по чужому платежу."""
    calls: list[str] = []

    async def fake_fulfill(payment_id: str) -> None:
        calls.append(payment_id)

    async def fake_get_payment(payment_id: str):
        return {"payment_id": payment_id, "telegram_id": 111, "status": "paid"}

    monkeypatch.setattr(bot, "fulfill_payment", fake_fulfill)
    monkeypatch.setattr(web_app, "_get_session", lambda request: None)
    monkeypatch.setattr(web_app, "get_payment", fake_get_payment)

    response = await web_app.handle_pay_success(_request("pay-any"))
    assert response.status == 200
    assert calls == []


# ---------- C3: тарифный хендлер не перехватывает админ-кнопки ----------

async def test_plan_handler_ignores_admin_nav_buttons():
    """«➕ Продлить» — админ-кнопка, её не должен ловить nav_plan."""
    handlers = [
        h for h in bot.router.message.handlers if h.callback.__name__ == "nav_plan"
    ]
    assert handlers, "nav_plan не зарегистрирован"
    text_filter = handlers[0].filters[0]

    class Msg:
        def __init__(self, text: str):
            self.text = text

    assert await text_filter.call(Msg("➕ Продлить")) is False
    # реальные кнопки тарифов по-прежнему обрабатываются
    assert await text_filter.call(Msg("➕ 30 дней")) is True
    assert await text_filter.call(Msg("💳 Тест")) is True


# ---------- C4: конфликт портов ----------

def test_duplicate_ports_rejected():
    errors = _duplicate_port_errors({"WEB_PORT": 8080, "WEBHOOK_PORT": 8080})
    assert errors
    assert "WEB_PORT" in errors[0] and "WEBHOOK_PORT" in errors[0]


def test_distinct_ports_accepted():
    assert _duplicate_port_errors({"WEB_PORT": 8080, "WEBAPP_PORT": 8082, "HEALTH_PORT": 8081}) == []


# ---------- C5: льготный период автопродления ----------

def test_autorenew_key_is_not_deleted_during_grace_period():
    now = 1_000_000_000
    customer = {
        "expires_at": now,
        "auto_renew": 1,
        "payment_method_id": "pm_1",
    }
    assert bot._expired_key_cleanup_due(customer, now) is False
    assert bot._expired_key_cleanup_due(customer, now + bot.AUTORENEW_GRACE_SECONDS - 1) is False
    assert bot._expired_key_cleanup_due(customer, now + bot.AUTORENEW_GRACE_SECONDS + 1) is True


def test_expired_key_deleted_without_autorenew():
    now = 1_000_000_000
    assert bot._expired_key_cleanup_due(
        {"expires_at": now - 60, "auto_renew": 0, "payment_method_id": "pm_1"}, now
    ) is True
    assert bot._expired_key_cleanup_due(
        {"expires_at": now - 60, "auto_renew": 1, "payment_method_id": None}, now
    ) is True
