"""Выбор сервера WDTT для нового клиента: наименее загруженный (least-loaded).

Используется и ботом (bot.py), и сайтом (web_app.py), чтобы распределение
пользователей работало одинаково из любого интерфейса.
"""
from __future__ import annotations

import config
from db import list_servers, server_active_counts


async def pick_least_loaded_server_id() -> int | None:
    """Сервер с наименьшим числом активных клиентов, но не выше лимита SERVER_MAX_USERS.

    Если все серверы заполнены до лимита — возвращает None (админ получит
    понятную ошибку и добавит новый сервер).
    """
    servers = await list_servers()
    if not servers:
        return None
    counts = await server_active_counts()
    available = [s for s in servers if counts.get(s["id"], 0) < config.SERVER_MAX_USERS]
    if not available:
        return None
    return min(available, key=lambda s: (counts.get(s["id"], 0), s["id"]))["id"]
