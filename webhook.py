"""Необязательный HTTP-webhook для уведомлений ЮKassa.

ЮKassa шлёт POST-уведомления вида:
    {"type": "notification", "event": "payment.succeeded", "object": {...}}

Мы не доверяем данным уведомления и перепроверяем платёж через API
(функция fetch_payment в bot.fulfill_payment). Уведомление лишь служит триггером.
"""
import asyncio
import logging
from typing import Awaitable, Callable

from aiohttp import web

log = logging.getLogger(__name__)

OnSucceeded = Callable[[str], Awaitable[None]]


def make_webhook_app(path: str, on_succeeded: OnSucceeded) -> web.Application:
    async def handler(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="bad json")

        if data.get("type") == "notification" and data.get("event") == "payment.succeeded":
            obj = data.get("object") or {}
            payment_id = obj.get("id")
            if payment_id:
                asyncio.create_task(on_succeeded(payment_id))

        return web.Response(text="ok")

    app = web.Application()
    app.router.add_post(path, handler)
    return app
