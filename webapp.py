"""Telegram Mini App (WebApp): REST API + раздача статики.

Архитектура:
- Фронтенд (static/) открывается внутри Telegram через window.Telegram.WebApp.
- Auth: POST /api/auth с initData от Telegram -> HMAC-валидация подписи -> сессионный токен.
- Все /api/* требуют заголовок Authorization: Bearer <token>.
- Логика (выдача доступа, ссылки, оплата) переиспользует bot.py через lazy-импорт,
  чтобы не было циклического импорта.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from urllib.parse import parse_qsl

from aiohttp import web

import config
from api import (  # единый API экосистемы: сессии, initData, /api/v1/*
    SESSION_TTL,
    make_session,
    register_v1_routes,
    validate_init_data,
    verify_session,
)
from db import (
    add_payment,
    claim_trial,
    get_customer,
    get_payment,
    get_plan,
    get_referrals_count,
    has_used_trial,
    list_plans,
    mark_trial_used,
    release_trial,
)

log = logging.getLogger("webapp")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# ---------- auth ----------
# make_session / verify_session / validate_init_data / SESSION_TTL — из api.py (общий модуль экосистемы)

# ---------- helpers ----------

def _user_id(request: web.Request) -> int:
    """Достаёт и проверяет сессию из заголовка Authorization. Бросает 401."""
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    uid = verify_session(token, config.TELEGRAM_TOKEN)
    if uid is None:
        raise web.HTTPUnauthorized(text="bad session")
    return uid


async def _json_body(request: web.Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ---------- API handlers ----------

async def api_auth(request: web.Request) -> web.Response:
    body = await _json_body(request)
    init_data = str(body.get("initData") or "")
    user = validate_init_data(init_data, config.TELEGRAM_TOKEN)
    if not user or not user.get("id"):
        return web.json_response(
            {"error": "Откройте приложение внутри Telegram (через кнопку в боте) — "
                      "в браузере Telegram не передаёт данные аккаунта."},
            status=401,
        )
    uid = int(user["id"])
    token = make_session(uid, config.TELEGRAM_TOKEN)
    return web.json_response({
        "token": token,
        "user": {
            "id": uid,
            "first_name": user.get("first_name", ""),
            "username": user.get("username", ""),
        },
        "stars_enabled": False,
        "minapp_url": config.MINIAPP_URL,
    })


async def api_me(request: web.Request) -> web.Response:
    """Профиль: подписка, трафик из панели, ссылки подключения."""
    uid = _user_id(request)
    import bot as b  # lazy: избегаем циклического импорта
    now = int(time.time())
    customer = await get_customer(uid)
    result = {
        "telegram_id": uid,
        "has_subscription": bool(customer and customer.get("wdtt_password")),
        "trial_used": await has_used_trial(uid),
    }
    if customer and customer.get("wdtt_password"):
        exp = int(customer.get("expires_at") or 0)
        plan = await get_plan(customer.get("plan_id")) or {}
        if not plan:
            from plans import ALL_PLANS_BY_ID
            plan = ALL_PLANS_BY_ID.get(customer.get("plan_id") or "") or {}
        result.update({
            "active": exp > now,
            "expires_at": exp,
            "plan": plan,
            "is_trial": (customer.get("plan_id") == "trial"),
            "server_id": customer.get("server_id"),
            "sub_id": customer.get("sub_id"),
        })
        # трафик и ссылки из панели
        try:
            client = await b.get_client(customer.get("server_id"))
            info = await client.get_user_info(customer["wdtt_password"]) or {}
            used = int(info.get("up_bytes") or 0) + int(info.get("down_bytes") or 0)
            limit_gb = int(plan.get("traffic_gb") or 0)
            result["traffic"] = {
                "used_bytes": used,
                "limit_bytes": int(limit_gb * 1024**3) if limit_gb else 0,
                "unlimited": limit_gb == 0,
            }
            links = await b.links_json(client, customer["wdtt_password"], customer.get("sub_id"))
            result["links"] = links
        except Exception as e:
            log.warning("api_me: панель недоступна для tg %s: %s", uid, e)
            result["links"] = None
            result["traffic"] = None
    return web.json_response(result)


async def api_plans(request: web.Request) -> web.Response:
    _user_id(request)
    from plans import DEVICE_ADDONS, PLANS, TRIAL
    plans = await list_plans()
    if not plans:
        plans = PLANS
    out = []
    for p in plans:
        out.append({
            "id": p["id"], "name": p["name"], "days": p["days"],
            "price": p["price"], "traffic_gb": p.get("traffic_gb", 0),
            "max_devices": p.get("max_devices", 1), "description": p.get("description", ""),
            "addon": False,
        })
    addons = []
    for p in DEVICE_ADDONS:
        addons.append({
            "id": p["id"], "name": p["name"], "days": p["days"],
            "price": p["price"], "traffic_gb": p.get("traffic_gb", 0),
            "max_devices": p.get("max_devices", 1), "description": p.get("description", ""),
            "addon": True,
        })
    return web.json_response({"plans": out, "addons": addons, "trial": TRIAL, "stars_enabled": False})


async def api_trial(request: web.Request) -> web.Response:
    """Активация пробного периода."""
    uid = _user_id(request)
    import bot as b
    from plans import TRIAL
    if await has_used_trial(uid):
        return web.json_response({"error": "trial already used"}, status=409)
    if not await claim_trial(uid):
        return web.json_response({"error": "trial already used"}, status=409)
    try:
        client, pwd, expires = await b.grant_access(uid, None, TRIAL)
        await mark_trial_used(uid)
        links = await b.links_json(client, pwd, (await get_customer(uid) or {}).get("sub_id"))
        return web.json_response({"ok": True, "expires_at": expires, "links": links})
    except Exception as e:
        await release_trial(uid)
        log.exception("api_trial: tg %s", uid)
        return web.json_response({"error": str(e)}, status=500)


async def api_pay_yookassa(request: web.Request) -> web.Response:
    """Создаёт платёж ЮKassa, возвращает confirmation_url."""
    uid = _user_id(request)
    body = await _json_body(request)
    plan_id = str(body.get("plan_id") or "")
    plan = await get_plan(plan_id)
    if not plan:
        from plans import ALL_PLANS_BY_ID
        plan = ALL_PLANS_BY_ID.get(plan_id)
    if not plan or plan_id == "trial":
        return web.json_response({"error": "plan not found"}, status=404)
    import payments as yk
    import bot as b
    customer = await get_customer(uid)
    username = None
    # достаём username из сессии нельзя — берём из initData при auth; здесь None допустим
    try:
        p = await yk.create_payment(plan, uid)
    except Exception as e:
        log.exception("api_pay_yookassa: не удалось создать платёж (тариф %s)", plan_id)
        return web.json_response(
            {"error": "Не удалось создать платёж. Попробуйте ещё раз через минуту."},
            status=502,
        )
    await add_payment(uid, username, p["id"], plan["id"], f"{float(plan['price']):.2f}")
    return web.json_response({"url": p["url"], "payment_id": p["id"]})


async def api_pay_stars(request: web.Request) -> web.Response:
    """Оплата Telegram Stars в мини-приложении отключена.

    Инвойсы Stars выдаёт только бот (кнопка в чате) — в мини-приложении
    доступна оплата картой/СБП через ЮKassa.
    """
    return web.json_response({"error": "stars disabled"}, status=400)


async def api_referrals(request: web.Request) -> web.Response:
    uid = _user_id(request)
    import bot as b
    count = await get_referrals_count(uid)
    username = config.TELEGRAM_BOT_USERNAME or b.bot_username or "bot"
    return web.json_response({
        "link": f"https://t.me/{username}?start=ref_{uid}",
        "count": count,
        "reward_days": config.REFERRAL_REWARD_DAYS,
    })


async def api_check_payment(request: web.Request) -> web.Response:
    """Пользователь вернулся после оплаты — проверяем все pending платежи."""
    uid = _user_id(request)
    import bot as b
    from db import get_pending_payments
    for p in await get_pending_payments():
        if int(p["telegram_id"]) == uid:
            try:
                await b.fulfill_payment(p["payment_id"])
            except Exception:
                log.exception("api_check_payment")
    customer = await get_customer(uid)
    return web.json_response({"has_subscription": bool(customer and customer.get("wdtt_password"))})


# ---------- app ----------

@web.middleware
async def error_logger_mw(request: web.Request, handler):
    """Логирует необработанные исключения (HTTP 500) с путём и текстом ошибки."""
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception:
        log.exception("HTTP 500 на %s %s", request.method, request.path)
        return web.json_response(
            {"error": "Внутренняя ошибка сервера. Попробуйте ещё раз через минуту."},
            status=500,
        )


def make_webapp() -> web.Application:
    app = web.Application(middlewares=[error_logger_mw])
    app.router.add_post("/api/auth", api_auth)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/plans", api_plans)
    app.router.add_post("/api/trial", api_trial)
    app.router.add_post("/api/pay/yookassa", api_pay_yookassa)
    app.router.add_post("/api/pay/stars", api_pay_stars)
    app.router.add_get("/api/referrals", api_referrals)
    app.router.add_post("/api/check_payment", api_check_payment)

    # Единый API экосистемы /api/v1/* — общий с сайтом и будущими приложениями
    register_v1_routes(app)

    # статика
    async def handle_index(request: web.Request) -> web.Response:
        """Главная мини-аппа: подставляем бренд в title и window.__brand."""
        path = os.path.join(STATIC_DIR, "index.html")
        with open(path, "r", encoding="utf-8") as f:
            html_text = f.read()
        brand = getattr(config, "BRAND_NAME", "K.O. Gateway")
        html_text = html_text.replace(
            "<title>K.O. Gateway — подписка</title>",
            f"<title>{brand} — подписка</title>",
        )
        bot_username = (os.getenv("TELEGRAM_BOT_USERNAME") or "").strip()
        bot_url = f"https://t.me/{bot_username}" if bot_username else ""
        html_text = html_text.replace(
            "<body>",
            f'<body>\n<script>window.__brand = {json.dumps(brand)};'
            f'window.__siteUrl = {json.dumps(getattr(config, "SITE_URL", ""))};'
            f'window.__botUrl = {json.dumps(bot_url)};</script>',
            1,
        )
        return web.Response(text=html_text, content_type="text/html")

    app.router.add_get("/", handle_index)
    app.router.add_static("/static/", STATIC_DIR, show_index=False)
    return app


async def run_webapp() -> None:
    app = make_webapp()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.WEBAPP_HOST, config.WEBAPP_PORT)
    await site.start()
    log.info("Mini App слушает на http://%s:%s/ (публичный URL: %s)",
             config.WEBAPP_HOST, config.WEBAPP_PORT, config.MINIAPP_URL or "не задан MINIAPP_URL")
