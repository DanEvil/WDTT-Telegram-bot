"""Платежи ЮKassa через REST API (https://yookassa.ru/developers/api).

Вместо синхронного SDK используем httpx: нативная асинхронность (не блокирует
event loop бота) и меньше зависимостей. Аутентификация — Basic
(shopId:secretKey), каждый изменяющий запрос требует заголовок
Idempotence-Key.
"""
import logging
from datetime import datetime
from uuid import uuid4

import httpx

from config import RETURN_URL, YOOKASSA_SECRET_KEY, YOOKASSA_SHOP_ID

log = logging.getLogger(__name__)

API_URL = "https://api.yookassa.ru/v3"


def _iso_to_ts(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except ValueError:
        return None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
        timeout=httpx.Timeout(30.0),
    )


async def create_payment(plan: dict, telegram_id: int) -> dict:
    """Создаёт платёж с автоподтверждением (capture=True).

    Возвращает dict: {"id": ..., "status": ..., "url": ссылка на оплату}.
    """
    amount = f"{float(plan['price']):.2f}"
    payload = {
        "amount": {"value": amount, "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": RETURN_URL},
        "capture": True,
        "description": f"WDTT — {plan['name']} ({plan['days']} дн.)",
        "metadata": {"telegram_id": str(telegram_id), "plan_id": str(plan["id"])},
        # Для соблюдения 54-ФЗ (выдача чека) раскомментируйте и заполните receipt.
        # Подробнее: https://yookassa.ru/developers/using-api/receipts
        # "receipt": {
        #     "customer": {"email": "user@example.com"},
        #     "items": [{
        #         "description": f"WDTT — {plan['name']}",
        #         "quantity": "1.00",
        #         "amount": {"value": amount, "currency": "RUB"},
        #         "vat_code": "1",
        #         "payment_mode": "full_payment",
        #         "payment_subject": "service",
        #     }],
        # },
    }
    async with _client() as http:
        r = await http.post(
            f"{API_URL}/payments",
            json=payload,
            headers={"Idempotence-Key": str(uuid4())},
        )
    if r.status_code >= 400:
        # ЮKassa возвращает описание ошибки в JSON: description + parameterViolations
        try:
            err = r.json().get("description") or r.text[:300]
        except ValueError:
            err = r.text[:300]
        raise RuntimeError(f"ЮKassa: не удалось создать платёж (HTTP {r.status_code}): {err}")
    p = r.json()
    url = ((p.get("confirmation") or {}).get("confirmation_url"))
    log.info("Создан платёж %s (тариф %s, %s ₽, tg %s)", p.get("id"), plan["id"], amount, telegram_id)
    return {"id": p.get("id"), "status": p.get("status"), "url": url}


async def fetch_payment(payment_id: str) -> dict | None:
    """Актуальное состояние платежа: {"id", "status", "paid_at", "amount", "metadata"}."""
    async with _client() as http:
        r = await http.get(f"{API_URL}/payments/{payment_id}")
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise RuntimeError(f"ЮKassa: HTTP {r.status_code} при получении платежа {payment_id}")
    p = r.json()
    amount = ((p.get("amount") or {}).get("value"))
    paid_at = p.get("captured_at") or p.get("paid_at")
    return {
        "id": p.get("id"),
        "status": p.get("status"),
        "paid_at": _iso_to_ts(p.get("captured_at") or p.get("paid_at")),
        "amount": amount,
        "metadata": dict(p.get("metadata") or {}),
    }
