"""Необязательный HTTP-webhook для уведомлений ЮKassa.

ЮKassa шлёт POST-уведомления вида:
    {"type": "notification", "event": "payment.succeeded", "object": {...}}

Мы не доверяем данным уведомления и перепроверяем платёж через API
(функция fetch_payment в bot.fulfill_payment). Уведомление лишь служит триггером.

Защита: грубый per-IP рейт-лимит, валидация формата payment_id,
дедупликация параллельных уведомлений об одном платёжe, ссылки на задачи
сохраняются (иначе задача может быть собрана GC).
"""
import asyncio
import ipaddress
import logging
import re
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

log = logging.getLogger(__name__)

OnSucceeded = Callable[[str], Awaitable[None]]

# id платежа ЮKassa: uuid-подобная строка из hex и дефисов
_PAYMENT_ID_RE = re.compile(r"^[0-9a-f][0-9a-f-]{15,63}$", re.IGNORECASE)

# Официальные IP-диапазоны ЮKassa для webhook-уведомлений
# (https://yookassa.ru/developers/using-api/webhooks#ip)
YOOKASSA_IP_NETWORKS = [
    ipaddress.ip_network(n) for n in (
        "185.71.76.0/27",
        "185.71.77.0/27",
        "77.75.153.0/25",
        "77.75.156.11/32",
        "77.75.156.35/32",
        "77.75.154.128/25",
        "2a02:5180::/32",
    )
]


def is_yookassa_ip(ip: str) -> bool:
    """Проверяет, что IP входит в официальные диапазоны ЮKassa."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in YOOKASSA_IP_NETWORKS)

# грубый рейт-лимит на один IP
_RATE_WINDOW = 60.0
_RATE_MAX = 30


def make_webhook_app(path: str, on_succeeded: OnSucceeded, check_ip: bool = True) -> web.Application:
    tasks: set[asyncio.Task] = set()      # текущие задачи (не дают их собрать GC)
    seen: deque[str] = deque(maxlen=256)  # недавно принятые платёжи (дедупликация)
    hits_by_ip: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=_RATE_MAX))

    async def run(payment_id: str) -> None:
        try:
            await on_succeeded(payment_id)
        except Exception:
            log.exception("webhook: обработка платежа %s упала", payment_id)

    async def handler(request: web.Request) -> web.Response:
        peer = request.remote or "?"

        # IP-whitelist ЮKassa (отключается только для локальной разработки)
        if check_ip and not is_yookassa_ip(peer):
            log.warning("webhook: отклонён запрос с не-ЮKassa IP %s", peer)
            return web.Response(status=403, text="forbidden")

        now = time.monotonic()
        hits = hits_by_ip[peer]
        while hits and now - hits[0] > _RATE_WINDOW:
            hits.popleft()
        if len(hits) >= _RATE_MAX:
            log.warning("webhook: превышен частотный лимит с %s", peer)
            return web.Response(status=429, text="too many requests")
        hits.append(now)

        try:
            data: Any = await request.json()
        except Exception:
            return web.Response(status=400, text="bad json")

        if not isinstance(data, dict):
            return web.Response(status=400, text="bad payload")
        if data.get("type") != "notification" or data.get("event") != "payment.succeeded":
            return web.Response(text="ok")

        obj = data.get("object") or {}
        payment_id = obj.get("id")
        if not isinstance(payment_id, str) or not _PAYMENT_ID_RE.match(payment_id):
            log.warning("webhook: некорректный payment_id с %s", peer)
            return web.Response(text="ok")
        if payment_id in seen:
            return web.Response(text="ok")  # дубль уже в обработке

        seen.append(payment_id)
        try:
            import metrics as m
            m.inc_counter(m.WEBHOOK_RECEIVED)
        except ImportError:
            pass
        task = asyncio.create_task(run(payment_id))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_post(path, handler)
    return app
