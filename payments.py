"""Платежи ЮKassa через REST API (https://yookassa.ru/developers/api).

Вместо синхронного SDK используем httpx: нативная асинхронность (не блокирует
event loop бота) и меньше зависимостей. Аутентификация — Basic
(shopId:secretKey), каждый изменяющий запрос требует заголовок
Idempotence-Key.
"""
import json
import logging
from datetime import datetime
from uuid import uuid4

import httpx

from config import RETURN_URL, YOOKASSA_SECRET_KEY, YOOKASSA_SHOP_ID

log = logging.getLogger(__name__)

# Состояние магазина по рекуррентным платежам. None = неизвестно, True/False — известно.
# ЮKassa отвечает 403 "This store can't make recurring payments", если магазин
# не включил рекурренты — тогда карта не сохраняется и автопродление невозможно.
_recurring_supported: bool | None = None


def recurring_supported() -> bool | None:
    """Поддерживает ли магазин рекуррентные платежи (None — ещё не проверяли)."""
    return _recurring_supported


def _set_recurring_supported(value: bool) -> None:
    global _recurring_supported
    if _recurring_supported != value:
        _recurring_supported = value
        log.info("ЮKassa: рекуррентные платежи %s",
                 "поддерживаются" if value else "не поддерживаются магазином")


async def probe_recurring_support() -> bool | None:
    """Проверяет поддержку рекуррентных платежей магазином ЮKassa.

    Если магазин их не поддерживает — API отвечает 403 и платёж НЕ создаётся
    (никакого мусора в ЛК). Если поддерживает — создаём тестовый платёж на 1 ₽
    и сразу отменяем его.
    """
    global _recurring_supported
    if _recurring_supported is not None:
        return _recurring_supported
    payload = {
        "amount": {"value": "1.00", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": RETURN_URL},
        "capture": True,
        "description": "K.O. Gateway — проверка автопродления",
        "metadata": {"telegram_id": "0", "plan_id": "recurring_probe"},
        "save_payment_method": True,
    }
    http = await get_http_client()
    try:
        r = await http.post(
            f"{API_URL}/payments",
            json=payload,
            headers={"Idempotence-Key": str(uuid4())},
        )
    except Exception as e:
        log.warning("ЮKassa: не удалось проверить рекуррентные платежи: %s", e)
        return None
    if r.status_code == 403 and "recurring" in (r.text or "").lower():
        _set_recurring_supported(False)
        return False
    if r.status_code < 400:
        _set_recurring_supported(True)
        # гасим тестовый платёж, чтобы не оставлять мусор в ЛК
        try:
            pid = (r.json() or {}).get("id")
            if pid:
                await http.post(f"{API_URL}/payments/{pid}/cancel", json={})
        except Exception:
            log.warning("ЮKassa: не удалось отменить тестовый платёж проверки", exc_info=True)
        return True
    log.warning("ЮKassa: проверка рекуррентов вернула HTTP %s", r.status_code)
    return None

API_URL = "https://api.yookassa.ru/v3"

# Общий HTTP-клиент (пул соединений) — создаётся один раз, переиспользуется
_http_client: httpx.AsyncClient | None = None


async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
            timeout=httpx.Timeout(30.0),
        )
    return _http_client


async def close_http_client() -> None:
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


def _iso_to_ts(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except ValueError:
        return None


async def create_payment(plan: dict, telegram_id: int, save_payment_method: bool = True,
                        return_url: str | None = None) -> dict:
    """Создаёт платёж с автоподтверждением (capture=True).

    save_payment_method=True — просим ЮKassa сохранить способ оплаты (для автопродления).
    return_url — куда вернуть пользователя после оплаты (по умолчанию RETURN_URL).
    Возвращает dict: {"id": ..., "status": ..., "url": ссылка на оплату}.
    """
    amount = f"{float(plan['price']):.2f}"
    payload = {
        "amount": {"value": amount, "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": return_url or RETURN_URL},
        "capture": True,
        "description": f"WDTT — {plan['name']} ({plan['days']} дн.)",
        "metadata": {"telegram_id": str(telegram_id), "plan_id": str(plan["id"])},
    }
    if save_payment_method:
        # ЮKassa больше не принимает save_payment_method="allowed".
        # Сохранение карты требует разрешения рекуррентных платежей у магазина;
        # если их нет — API отвечает 403, и платёж повторяется без сохранения карты.
        payload["save_payment_method"] = True
    # Для соблюдения 54-ФЗ (выдача чека) раскомментируйте и заполните receipt:
    # "receipt": {"customer": {"email": "user@example.com"}, "items": [{...}]}
    # Подробнее: https://yookassa.ru/developers/using-api/receipts
    http = await get_http_client()
    r = await http.post(
        f"{API_URL}/payments",
        json=payload,
        headers={"Idempotence-Key": str(uuid4())},
    )
    if r.status_code == 403 and save_payment_method and "recurring" in (r.text or "").lower():
        # магазин не поддерживает рекуррентные платежи — платим без сохранения карты
        _set_recurring_supported(False)
        log.warning(
            "ЮKassa: магазин не поддерживает рекуррентные платежи (403), "
            "платёж создаётся без сохранения карты — автопродление недоступно"
        )
        payload.pop("save_payment_method", None)
        r = await http.post(
            f"{API_URL}/payments",
            json=payload,
            headers={"Idempotence-Key": str(uuid4())},
        )
    elif r.status_code < 400 and save_payment_method:
        # магазин принял save_payment_method — рекурренты работают
        _set_recurring_supported(True)
    if r.status_code >= 400:
        # ЮKassa возвращает описание ошибки в JSON: description + parameterViolations
        try:
            body = r.json()
            err = body.get("description") or r.text[:300]
            if body.get("parameter"):
                err += f" (parameter={body['parameter']})"
            viol = body.get("parameterViolations")
            if viol:
                err += " " + json.dumps(viol, ensure_ascii=False)[:300]
        except ValueError:
            err = r.text[:300]
        raise RuntimeError(f"ЮKassa: не удалось создать платёж (HTTP {r.status_code}): {err}")
    p = r.json()
    url = ((p.get("confirmation") or {}).get("confirmation_url"))
    log.info("Создан платёж %s (тариф %s, %s ₽, tg %s)", p.get("id"), plan["id"], amount, telegram_id)
    return {"id": p.get("id"), "status": p.get("status"), "url": url}


async def fetch_payment(payment_id: str) -> dict | None:
    """Актуальное состояние платежа: {"id", "status", "paid_at", "amount", "metadata"}."""
    http = await get_http_client()
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
        "payment_method_id": p.get("payment_method_id"),
        "confirmation_url": (p.get("confirmation") or {}).get("confirmation_url"),
    }


async def charge_saved_method(telegram_id: int, amount_rub: float, description: str,
                              plan_id: str = "month",
                              idempotency_key: str | None = None) -> dict:
    """Безакцептное списание с сохранённого способа оплаты (автопродление).

    Использует payment_method_id, сохранённый при первой оплате. Без страницы
    подтверждения — capture=True. Возвращает {"id", "status"}.

    idempotency_key — стабильный ключ идемпотентности: при повторе запроса
    ЮKassa вернёт тот же платёж, а не спишет деньги второй раз.
    """
    from db import get_customer
    customer = await get_customer(telegram_id)
    pm_id = (customer or {}).get("payment_method_id")
    if not pm_id:
        raise RuntimeError("нет сохранённого способа оплаты — оплатите картой хотя бы раз")
    payload = {
        "amount": {"value": f"{float(amount_rub):.2f}", "currency": "RUB"},
        "capture": True,
        "description": description,
        "payment_method_id": pm_id,
        "metadata": {"telegram_id": str(telegram_id), "plan_id": plan_id, "auto": "1"},
    }
    http = await get_http_client()
    r = await http.post(
        f"{API_URL}/payments",
        json=payload,
        headers={"Idempotence-Key": idempotency_key or str(uuid4())},
    )
    if r.status_code >= 400:
        try:
            err = r.json().get("description") or r.text[:300]
        except ValueError:
            err = r.text[:300]
        raise RuntimeError(f"ЮKassa: не удалось списать с сохранённой карты (HTTP {r.status_code}): {err}")
    p = r.json()
    log.info("Автосписание %s ₽ с сохранённой карты (tg %s): платёж %s, статус %s",
             amount_rub, telegram_id, p.get("id"), p.get("status"))
    return {"id": p.get("id"), "status": p.get("status")}
