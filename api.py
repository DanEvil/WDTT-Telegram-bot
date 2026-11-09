"""Единый публичный API экосистемы K.O. Gateway.

Один и тот же набор эндпоинтов монтируется и на сайте (web_app.py),
и в мини-аппе (webapp.py), и в будущих нативных приложениях:

  POST /api/v1/auth            {initData} -> {token}   (Telegram WebApp / Login Widget)
  GET  /api/v1/ecosystem       бренд + все точки входа (бот, сайт, мини-апп, приложения)
  GET  /api/v1/plans           тарифы + аддоны устройств + пробный период
  GET  /api/v1/me              Bearer-токен -> подписка, трафик, ссылки подключения
  POST /api/v1/create-payment  {plan_id} -> ссылка ЮKassa
  GET  /api/v1/check-payment   ?payment_id=... -> статус

Auth: сессионный токен (HMAC bot_token), живёт 7 дней. Для нативных
приложений — тот же механизм, что и у мини-аппа: initData от Telegram.
"""
import base64
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl, unquote

from aiohttp import web

import config

log = logging.getLogger("api")

SESSION_TTL = 86400 * 7  # 7 дней


# ---------- сессии и initData (общие для мини-аппа и приложений) ----------

def _sign(payload: dict, bot_token: str) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(bot_token.encode(), body.encode(), hashlib.sha256).digest()
    return body + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")


def make_session(user_id: int, bot_token: str) -> str:
    return _sign({"uid": user_id, "exp": int(time.time()) + SESSION_TTL}, bot_token)


def verify_session(token: str, bot_token: str) -> int | None:
    try:
        body, sig = token.split(".", 1)
        expect = base64.urlsafe_b64encode(
            hmac.new(bot_token.encode(), body.encode(), hashlib.sha256).digest()
        ).decode().rstrip("=")
        if not hmac.compare_digest(expect, sig):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if payload.get("exp", 0) < time.time():
            return None
        return int(payload["uid"])
    except Exception:
        return None


def _init_data_variants(init_data: str) -> list[dict]:
    """Варианты разбора initData.

    Основной — стандартный query-разбор (unquote_plus превращает '+' в пробел).
    Запасной — ручной разбор с unquote (без замены '+' на пробел): если в полях
    Telegram реально встречается '+' (например, в JSON пользователя), первый
    вариант даст другое значение и хеш не сойдётся.
    """
    variants: list[dict] = []
    try:
        variants.append(dict(parse_qsl(init_data, keep_blank_values=True)))
    except Exception:
        pass
    alt: dict[str, str] = {}
    for part in init_data.split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        alt[unquote(k)] = unquote(v)
    if alt and alt not in variants:
        variants.append(alt)
    return variants


def _init_data_secrets(bot_token: str) -> list[bytes]:
    """Варианты секретного ключа для подписи initData.

    Документация Telegram противоречива, поэтому пробуем все известные схемы:
    1. HMAC_SHA256(key=bot_token, msg="WebAppData") — актуальный Mini Apps (core.telegram.org)
    2. HMAC_SHA256(key="WebAppData", msg=bot_token) — вариант из docs.telegram-mini-apps.com
    3. SHA256(bot_token) — старая схема / Telegram Login Widget
    """
    t = bot_token.encode()
    return [
        hmac.new(t, b"WebAppData", hashlib.sha256).digest(),
        hmac.new(b"WebAppData", t, hashlib.sha256).digest(),
        hashlib.sha256(t).digest(),
    ]


def _check_strings(pairs: dict, bot_token: str) -> list[str]:
    """Варианты data-check-string.

    1. Просто пары, отсортированные по ключу (классика).
    2. С префиксом "<bot_id>:WebAppData\\n" — новый формат подписи initData
       в свежих клиентах Telegram (Bot API 9.x+).
    """
    base = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    bot_id = bot_token.split(":", 1)[0]
    return [base, f"{bot_id}:WebAppData\n{base}"]


def validate_init_data(init_data: str, bot_token: str) -> dict | None:
    """Валидирует initData от Telegram WebApp / Login Widget. Возвращает объект user или None."""
    if not init_data:
        return None
    received_hash = None
    calc = ""
    secrets = _init_data_secrets(bot_token)
    matched = False
    pairs: dict = {}
    for variant in _init_data_variants(init_data):
        received_hash = variant.pop("hash", None)
        if not received_hash:
            return None
        # Bot API 8.0+ добавил поле signature (Ed25519 для сторонней проверки).
        # Оно НЕ входит в data-check-string для hash, поэтому его нужно исключить,
        # иначе проверка подписи не сойдётся у современных клиентов Telegram.
        variant.pop("signature", None)
        pairs = variant
        for check_string in _check_strings(variant, bot_token):
            for secret in secrets:
                calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
                if hmac.compare_digest(calc, received_hash):
                    matched = True
                    break
            if matched:
                break
        if matched:
            break
    if not matched:
        # ни один вариант не сошёлся — логируем причину без самих данных
        _log_init_failure(init_data, received_hash, calc)
        return None
    try:
        if abs(time.time() - int(pairs.get("auth_date", "0"))) > 86400:
            return None  # протухший initData
    except ValueError:
        return None
    try:
        return json.loads(pairs.get("user", "{}"))
    except ValueError:
        return None


def _log_init_failure(init_data: str, received_hash: str | None, calc: str) -> None:
    """Диагностика отказа валидации: только метаданные, без данных пользователя."""
    try:
        fields = [p.split("=", 1)[0] for p in init_data.split("&") if p]
    except Exception:
        fields = []
    log.warning(
        "initData: подпись не сошлась (len=%s, поля=%s, есть hash=%s, есть user=%s, "
        "есть signature=%s, расхождение=%s)",
        len(init_data), sorted(fields), bool(received_hash),
        "user" in fields, "signature" in fields,
        "совпало" if received_hash == calc else "не совпало",
    )


def user_id_from_request(request: web.Request) -> int:
    """Bearer-токен -> telegram_id. Бросает 401."""
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    uid = verify_session(token, config.TELEGRAM_TOKEN)
    if uid is None:
        raise web.HTTPUnauthorized(text="bad session")
    return uid


# ---------- полезные нагрузки ----------

def bot_url(start: str = "") -> str:
    if not config.TELEGRAM_BOT_USERNAME:
        return ""
    return f"https://t.me/{config.TELEGRAM_BOT_USERNAME}" + (f"?start={start}" if start else "")


def ecosystem_payload() -> dict:
    """Все точки входа экосистемы в одном ответе — для сайта, приложений, бота."""
    return {
        "brand": config.BRAND_NAME,
        "bot": {
            "url": bot_url(),
            "bind_deep_link": bot_url("bind_<code>"),   # шаблон: код из /bind-telegram на сайте
            "ref_deep_link": bot_url("ref_<tg_id>"),
        },
        "site": {"url": config.SITE_URL},
        "miniapp": {"url": config.MINIAPP_URL},
        "apps": {
            "android": config.APP_ANDROID_URL,
            "ios_testflight": config.APP_IOS_TESTFLIGHT_URL,
            "ios_join": config.APP_IOS_JOIN_URL,
            "windows": config.APP_WINDOWS_URL,
        },
        "api": {"version": "v1", "base": "/api/v1"},
    }


def _plan_json(p: dict, addon: bool) -> dict:
    return {
        "id": p["id"], "name": p["name"], "days": p["days"],
        "price": float(p["price"]), "currency": "RUB",
        "traffic_gb": p.get("traffic_gb", 0),
        "max_devices": p.get("max_devices", 1),
        "description": p.get("description", ""),
        "addon": addon,
    }


async def plans_payload() -> dict:
    from plans import DEVICE_ADDONS, PLANS, TRIAL
    from db import list_plans
    plans = await list_plans() or PLANS
    return {
        "brand": config.BRAND_NAME,
        "plans": [_plan_json(p, False) for p in plans],
        "addons": [_plan_json(p, True) for p in DEVICE_ADDONS],
        "trial": _plan_json(TRIAL, False) | {"price": 0, "free": True},
        # оплата Telegram Stars в мини-приложении отключена
        "stars_enabled": False,
    }


async def status_payload(uid: int) -> dict:
    """Статус подписки пользователя: план, срок, трафик из панели, ссылки."""
    from db import get_customer, get_plan, has_used_trial
    from plans import ALL_PLANS_BY_ID
    now = int(time.time())
    customer = await get_customer(uid)
    result = {
        "telegram_id": uid,
        "has_subscription": bool(customer and customer.get("wdtt_password")),
        "trial_used": await has_used_trial(uid),
        "brand": config.BRAND_NAME,
    }
    if customer and customer.get("wdtt_password"):
        exp = int(customer.get("expires_at") or 0)
        plan = await get_plan(str(customer.get("plan_id") or "")) or ALL_PLANS_BY_ID.get(str(customer.get("plan_id") or "")) or {}
        result.update({
            "active": exp > now,
            "expires_at": exp,
            "plan": plan,
            "is_trial": customer.get("plan_id") == "trial",
            "server_id": customer.get("server_id"),
            "sub_id": customer.get("sub_id"),
        })
        try:
            import bot as b  # lazy: избегаем циклического импорта
            client = await b.get_client(customer.get("server_id"))
            info = await client.get_user_info(customer["wdtt_password"]) or {}
            used = int(info.get("up_bytes") or 0) + int(info.get("down_bytes") or 0)
            limit_gb = int(plan.get("traffic_gb") or 0)
            result["traffic"] = {
                "used_bytes": used,
                "limit_bytes": int(limit_gb * 1024**3) if limit_gb else 0,
                "unlimited": limit_gb == 0,
            }
            result["max_devices"] = int(info.get("max_devices") or plan.get("max_devices") or 1)
            result["links"] = await b.links_json(client, customer["wdtt_password"], customer.get("sub_id"))
        except Exception as e:
            log.warning("api v1 me: панель недоступна для tg %s: %s", uid, e)
            result["links"] = None
            result["traffic"] = None
    return result


# ---------- обработчики ----------

async def h_auth(request: web.Request) -> web.Response:
    """POST {initData} -> {token, user, ecosystem}."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    user = validate_init_data(str(body.get("initData") or ""), config.TELEGRAM_TOKEN)
    if not user or not user.get("id"):
        return web.json_response(
            {"error": "Откройте приложение внутри Telegram (через кнопку в боте) — "
                      "в браузере Telegram не передаёт данные аккаунта."},
            status=401,
        )
    uid = int(user["id"])
    return web.json_response({
        "token": make_session(uid, config.TELEGRAM_TOKEN),
        "user": {"id": uid, "first_name": user.get("first_name", ""), "username": user.get("username", "")},
        "stars_enabled": False,
        "ecosystem": ecosystem_payload(),
    })


async def h_ecosystem(request: web.Request) -> web.Response:
    return web.json_response(ecosystem_payload())


async def h_plans(request: web.Request) -> web.Response:
    return web.json_response(await plans_payload())


async def h_me(request: web.Request) -> web.Response:
    return web.json_response(await status_payload(user_id_from_request(request)))


async def h_create_payment(request: web.Request) -> web.Response:
    """POST Bearer + {plan_id} -> {payment_id, url, amount, currency}."""
    uid = user_id_from_request(request)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    plan_id = str(body.get("plan_id") or "")
    from db import add_payment, get_plan, init_db
    from plans import ALL_PLANS_BY_ID
    from payments import create_payment
    await init_db()
    plan = await get_plan(plan_id) or ALL_PLANS_BY_ID.get(plan_id)
    if not plan or plan_id == "trial":
        return web.json_response({"error": "plan not found"}, status=404)
    try:
        payment = await create_payment(plan, uid)
    except Exception as e:
        log.exception("api v1 create-payment")
        return web.json_response({"error": str(e)}, status=500)
    if not payment.get("url"):
        return web.json_response({"error": "no payment url"}, status=500)
    await add_payment(telegram_id=uid, username=str(body.get("username") or ""),
                      payment_id=payment["id"], plan_id=str(plan["id"]),
                      amount=f"{float(plan['price']):.2f}")
    return web.json_response({
        "payment_id": payment["id"], "url": payment["url"],
        "amount": float(plan["price"]), "currency": "RUB", "plan": plan["name"],
    })


async def h_check_payment(request: web.Request) -> web.Response:
    """GET /api/v1/check-payment?payment_id=... -> статус платежа (только свой)."""
    uid = user_id_from_request(request)
    from db import get_payment, init_db
    from payments import fetch_payment
    payment_id = request.query.get("payment_id")
    if not payment_id:
        return web.json_response({"error": "missing payment_id"}, status=400)
    await init_db()
    row = await get_payment(payment_id)
    if not row:
        return web.json_response({"error": "not found"}, status=404)
    # платёж должен принадлежать запрашивающему — иначе можно узнать статус чужих платежей
    if int(row.get("telegram_id") or 0) != uid:
        return web.json_response({"error": "not found"}, status=404)
    try:
        yk = await fetch_payment(payment_id)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=502)
    status = (yk or {}).get("status") or row.get("status") or "pending"
    return web.json_response({"payment_id": payment_id, "status": status})


# ---------- настройки автопродления ----------

NOTIFY_HOURS_OPTIONS = [0, 1, 3, 6, 12, 24, 48]  # 0 = не уведомлять


async def h_settings_get(request: web.Request) -> web.Response:
    """GET /api/v1/settings — текущие настройки автопродления + цена месяца."""
    from db import get_auto_renew_settings, get_plan
    from plans import ALL_PLANS_BY_ID
    from payments import recurring_supported
    uid = user_id_from_request(request)
    settings = await get_auto_renew_settings(uid)
    month = await get_plan("month") or ALL_PLANS_BY_ID.get("month") or {}
    settings.update({
        "renew_days": 30,  # автопродление всегда только на 1 месяц
        "renew_price": float(month.get("price") or 0),
        "renew_plan": month.get("name", "Month"),
        "notify_hours_options": NOTIFY_HOURS_OPTIONS,
        "brand": config.BRAND_NAME,
        # False — магазин не поддерживает рекурренты, карта не сохранится
        # и автопродление технически невозможно (None — ещё не проверяли)
        "recurring_supported": recurring_supported(),
    })
    return web.json_response(settings)


async def h_settings_post(request: web.Request) -> web.Response:
    """POST /api/v1/settings {auto_renew: bool, notify_hours: int} — галочки автопродления и уведомления."""
    from db import set_auto_renew, get_auto_renew_settings
    uid = user_id_from_request(request)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    auto_renew = bool(body.get("auto_renew"))
    try:
        notify_hours = int(body.get("notify_hours") or 0)
    except (TypeError, ValueError):
        notify_hours = 0
    if notify_hours < 0:
        notify_hours = 0
    await set_auto_renew(uid, auto_renew, notify_hours)
    return web.json_response(await get_auto_renew_settings(uid) | {
        "renew_days": 30, "brand": config.BRAND_NAME,
    })


async def h_instructions(request: web.Request) -> web.Response:
    """Пошаговые инструкции по подключению со скриншотами (единый источник — instructions.py)."""
    import instructions as instr

    platforms = []
    for key, p in instr.PLATFORMS.items():
        platforms.append({
            "key": key,
            "title": p["title"],
            "intro": p["intro"],
            "app_url": p.get("app_url"),
            "app_label": p.get("app_label"),
            "links": [{"url": u, "label": l} for u, l in p.get("links", [])],
            "steps": [{"img": instr.step_img(img), "caption": cap} for img, cap in p["steps"]],
        })
    return web.json_response({"intro": instr.INTRO, "platforms": platforms})


def register_v1_routes(app: web.Application, prefix: str = "/api/v1") -> None:
    """Монтирует единый API экосистемы в aiohttp-приложение (сайт или мини-апп)."""
    app.router.add_post(f"{prefix}/auth", h_auth)
    app.router.add_get(f"{prefix}/ecosystem", h_ecosystem)
    app.router.add_get(f"{prefix}/plans", h_plans)
    app.router.add_get(f"{prefix}/me", h_me)
    app.router.add_post(f"{prefix}/create-payment", h_create_payment)
    app.router.add_get(f"{prefix}/check-payment", h_check_payment)
    app.router.add_get(f"{prefix}/settings", h_settings_get)
    app.router.add_post(f"{prefix}/settings", h_settings_post)
    app.router.add_get(f"{prefix}/instructions", h_instructions)
