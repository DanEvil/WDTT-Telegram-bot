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
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import smtplib
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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

    Telegram отдаёт query-строку, но разные клиенты кодируют значения по-разному,
    поэтому перебираем все известные варианты декодирования:
    1. parse_qsl — стандартный разбор (+ превращается в пробел);
    2. unquote — ручной разбор без замены '+' на пробел;
    3. сырые значения — как есть в строке (percent-encoded);
    4. сырые значения с заменой '+' на пробел.
    """
    variants: list[dict] = []

    def _add(d: dict) -> None:
        if d and d not in variants:
            variants.append(d)

    try:
        _add(dict(parse_qsl(init_data, keep_blank_values=True)))
    except Exception:
        pass

    alt: dict[str, str] = {}
    raw: dict[str, str] = {}
    for part in init_data.split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        alt[unquote(k)] = unquote(v)
        raw[k] = v
    _add(alt)
    _add(raw)
    _add({k: v.replace("+", " ") for k, v in raw.items()})
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
    Пары могут включать поле signature: в свежих клиентах (меню-приложение)
    оно входит в data-check-string, в старых — нет, поэтому обе версии
    перебираются в validate_init_data.
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
        # Bot API 8.0+ добавил поле signature (Ed25519). В одних клиентах оно
        # входит в data-check-string, в других — нет; перебираем оба варианта.
        pairs_sets = [
            {k: v for k, v in variant.items() if k != "signature"},
            variant,
        ]
        for pairs in pairs_sets:
            for check_string in _check_strings(pairs, bot_token):
                for secret in secrets:
                    calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
                    if hmac.compare_digest(calc, received_hash):
                        matched = True
                        break
                if matched:
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
    # Временная отладка: включается флагом DEBUG_INIT_DATA=1 (для подбора схемы подписи).
    if os.getenv("DEBUG_INIT_DATA") == "1":
        log.warning("initData DEBUG (base64): %s",
                    base64.b64encode(init_data.encode("utf-8")).decode("ascii"))


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


def ecosystem_payload(windows_url: str | None = None) -> dict:
    """Все точки входа экосистемы в одном ответе — для сайта, приложений, бота.

    windows_url — эффективная ссылка PWDTT из админ-панели (если None — из конфига).
    """
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
            "windows": windows_url or config.APP_WINDOWS_URL,
        },
        "api": {"version": "v1", "base": "/api/v1"},
    }


async def app_windows_url_effective() -> str:
    """Ссылка PWDTT (Windows) с учётом значения, заданного в админ-панели."""
    from db import get_app_windows_url
    return await get_app_windows_url()


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
        "is_admin": is_admin_uid(uid),
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
            result["devices_online"] = int(info.get("devices_online") or 0)
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
        "is_admin": is_admin_uid(uid),
        "stars_enabled": False,
        "ecosystem": ecosystem_payload(await app_windows_url_effective()),
    })

# ---------- коды подтверждения email (в памяти, живут 15 минут) ----------

_email_codes: dict[str, dict] = {}  # email -> {code, expires_at}

EMAIL_CODE_TTL = 900  # 15 минут

# Диапазон uid для email-аккаунтов: 10^9..2*10^9.
# Настоящие telegram_id меньше 10^9, поэтому пересечения с ними нет.
EMAIL_UID_BASE = 10 ** 9

def email_uid(email: str) -> int:
    """Стабильный числовой id пользователя по email.

    Использует sha256, а не hash(): встроенный hash() для строк
    рандомизируется при каждом запуске интерпретатора (PYTHONHASHSEED),
    из-за чего после рестарта сервера тот же email получал бы другой id
    и терял подписку, пробный период и привязки.
    """
    digest = hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()
    return EMAIL_UID_BASE + (int(digest[:12], 16) % (10 ** 9))

def _generate_email_code() -> str:
    import random
    return f"{random.randint(100000, 999999):06d}"

def _send_email_code(to_email: str, code: str) -> bool:
    """Отправляет код подтверждения на email.
    Если SMTP не настроен — пишет код в лог (для разработки)."""
    smtp_host = os.getenv("SMTP_HOST", "")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")
    smtp_from = os.getenv("SMTP_FROM", smtp_user or "no-reply@ko-gateway.ru")

    body = f"""\
Код подтверждения для входа в K.O. Gateway: <b>{code}</b>
Код действителен 15 минут.
Если вы не запрашивали этот код — проигнорируйте это письмо."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Код подтверждения входа — K.O. Gateway"
    msg["From"] = smtp_from
    msg["To"] = to_email
    msg.attach(MIMEText(body, "html"))

    if smtp_host and smtp_user and smtp_pass:
        try:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as s:
                s.starttls()
                s.login(smtp_user, smtp_pass)
                s.send_message(msg)
            return True
        except Exception:
            log.exception("SMTP send failed")
    # fallback: код в лог
    log.info(f"EMAIL CODE for {to_email}: {code}")
    return True

async def h_email_send_code(request: web.Request) -> web.Response:
    """POST /api/v1/auth/email/send-code — отправить код подтверждения на email."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    email = str(body.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return web.json_response({"error": "Укажите email"}, status=400)
    code = _generate_email_code()
    _email_codes[email] = {
        "code": code,
        "expires_at": time.time() + EMAIL_CODE_TTL,
    }
    if _send_email_code(email, code):
        return web.json_response({"ok": True, "email": email})
    return web.json_response({"error": "Не удалось отправить письмо"}, status=500)

async def h_email_verify(request: web.Request) -> web.Response:
    """POST /api/v1/auth/email/verify — проверить код и выдать токен."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    email = str(body.get("email") or "").strip().lower()
    code = str(body.get("code") or "").strip()
    if not email or not code:
        return web.json_response({"error": "Укажите email и код"}, status=400)
    record = _email_codes.get(email)
    if not record:
        return web.json_response({"error": "Код не найден. Запросите код повторно."}, status=400)
    if time.time() > record["expires_at"]:
        del _email_codes[email]
        return web.json_response({"error": "Код истёк. Запросите новый."}, status=400)
    if record["code"] != code:
        return web.json_response({"error": "Неверный код."}, status=400)
    # код верный — создаём пользователя/сессию
    # uid из email должен быть СТАБИЛЬНЫМ между перезапусками сервера,
    # иначе после рестарта пользователь получит другой id и потеряет
    # подписку/пробный период (hash() рандомизируется per-process).
    uid = email_uid(email)
    del _email_codes[email]
    return web.json_response({
        "token": make_session(uid, config.TELEGRAM_TOKEN),
        "user": {"id": uid, "email": email, "first_name": email.split("@")[0]},
        "is_admin": is_admin_uid(uid),
        "stars_enabled": False,
        "ecosystem": ecosystem_payload(await app_windows_url_effective()),
    })


async def h_claim_trial(request: web.Request) -> web.Response:
    """POST /api/v1/claim-trial — активировать пробный период на 3 дня."""
    uid = user_id_from_request(request)
    from db import claim_trial, has_used_trial, mark_trial_used, release_trial, init_db
    from plans import TRIAL
    await init_db()
    if await has_used_trial(uid):
        return web.json_response({"error": "Пробный период уже использован"}, status=409)
    if not await claim_trial(uid):
        return web.json_response({"error": "Пробный период уже использован"}, status=409)
    try:
        import bot as b  # lazy: избегаем циклического импорта
        client, pwd, expires = await b.grant_access(uid, None, TRIAL)
        await mark_trial_used(uid)
        links = await b.links_json(client, pwd, (await __import__("db").get_customer(uid) or {}).get("sub_id"))
        return web.json_response({"ok": True, "expires_at": expires, "plan": TRIAL, "links": links})
    except Exception as e:
        await release_trial(uid)
        log.exception("api claim-trial: uid %s", uid)
        return web.json_response({"error": str(e)}, status=500)


async def h_connections(request: web.Request) -> web.Response:
    """GET /api/v1/connections — ссылки подключения по аккаунту пользователя."""
    uid = user_id_from_request(request)
    from db import get_customer, get_user_connections, init_db
    await init_db()
    customer = await get_customer(uid)
    prefs = await get_user_connections(uid)
    out: dict = {}

    # WDTT: настоящая ссылка из панели (с паролем и vk_hash)
    if customer and customer.get("wdtt_password"):
        try:
            import bot as b  # lazy: избегаем циклического импорта
            client = await b.get_client(customer.get("server_id"))
            links = await b.links_json(client, customer["wdtt_password"], customer.get("sub_id"))
            if links:
                out["wdtt"] = links.get("windows") or links.get("universal") or links.get("android")
        except Exception as e:
            log.warning("h_connections: панель недоступна для uid %s: %s", uid, e)

    # AIVPN: адрес сервера из настроек пользователя
    aivpn_server = (prefs.get("aivpn_server") or "").strip()
    out["aivpn"] = f"aivpn://{aivpn_server}" if aivpn_server else ""

    # OpenFlux: параметры транспорта из настроек пользователя,
    # а если не заданы — глобальные дефолты из админ-панели/конфига
    from db import get_openflux_url, get_openflux_secret
    out["openflux"] = {
        "transport": prefs.get("of_transport") or "yandex",
        "url": prefs.get("of_url") or await get_openflux_url(),
        "secret": prefs.get("of_secret") or await get_openflux_secret(),
        "inbound": prefs.get("of_inbound") or "socks5",
        "codec": prefs.get("of_codec") or "batched",
    }
    return web.json_response(out)


async def h_connections_save(request: web.Request) -> web.Response:
    """POST /api/v1/connections/save — сохранить настройки подключения пользователя."""
    uid = user_id_from_request(request)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    from db import init_db, set_user_connections
    await init_db()
    saved = await set_user_connections(
        uid,
        aivpn_server=body.get("aivpn_server", ""),
        of_transport=body.get("of_transport", "yandex"),
        of_url=body.get("of_url", ""),
        of_secret=body.get("of_secret", ""),
        of_inbound=body.get("of_inbound", "socks5"),
        of_codec=body.get("of_codec", "batched"),
    )
    return web.json_response({"ok": True, "connections": saved})


async def h_block_methods(request: web.Request) -> web.Response:
    """GET /api/v1/block-methods — репозиторий запрета: каталог техник блокировки."""
    try:
        import block_methods as bm
    except ImportError:
        return web.json_response({"error": "catalog unavailable"}, status=503)
    return web.json_response({
        "brand": config.BRAND_NAME,
        "methods": bm.get_catalog(),
    })


async def h_block_diagnose(request: web.Request) -> web.Response:
    """POST /api/v1/block-diagnose — определить технику блокировки для сайта."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    url = str(body.get("url") or "").strip()
    if not url:
        return web.json_response({"error": "missing url"}, status=400)
    if "://" not in url:
        url = "https://" + url
    try:
        import block_methods as bm
    except ImportError:
        return web.json_response({"error": "catalog unavailable"}, status=503)
    threshold = int(body.get("threshold_ms") or 3000)
    try:
        diag = await asyncio.to_thread(bm.diagnose, url, threshold, 8.0)
    except Exception as e:
        log.exception("block-diagnose")
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response(diag)


async def h_ecosystem(request: web.Request) -> web.Response:
    return web.json_response(ecosystem_payload(await app_windows_url_effective()))


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

    plats = instr.platforms_with_windows_app(await app_windows_url_effective())
    platforms = []
    for key, p in plats.items():
        platforms.append({
            "key": key,
            "title": p["title"],
            "intro": p["intro"],
            "app_url": p.get("app_url"),
            "app_label": p.get("app_label"),
            "links": [{"url": u, "label": l} for u, l in p.get("links", [])],
            "steps": [{"img": instr.step_img(img) if img else None, "caption": cap} for img, cap in p["steps"]],
        })
    return web.json_response({"intro": instr.INTRO, "platforms": platforms})


# ---------- админ-панель (общая для сайта и мини-аппа) ----------

def is_admin_uid(uid: int) -> bool:
    """Админ = в конфиге ADMIN_IDS или назначен через админ-панель (БД)."""
    from db import is_admin_cached
    return uid in config.ADMIN_IDS or is_admin_cached(uid)


def _admin_only(uid: int) -> web.Response | None:
    """None, если uid — админ; иначе 403-ответ."""
    if not is_admin_uid(uid):
        return web.json_response({"error": "forbidden"}, status=403)
    return None


async def h_admin_overview(request: web.Request) -> web.Response:
    """GET /api/v1/admin — статистика, серверы с нагрузкой, последние установки. Только админ."""
    uid = user_id_from_request(request)
    deny = _admin_only(uid)
    if deny:
        return deny
    from db import init_db, list_servers, server_active_counts, stats
    import admin_ops
    await init_db()
    s = await stats()
    counts = await server_active_counts()
    servers = []
    for srv in await list_servers():
        servers.append({
            "id": srv["id"],
            "name": srv["name"],
            "base_url": srv["base_url"],
            "ssh_host": srv.get("ssh_host"),
            "ssh_port": srv.get("ssh_port"),
            "ssh_user": srv.get("ssh_user"),
            "active_users": counts.get(srv["id"], 0),
        })
    return web.json_response({
        "stats": s,
        "server_max_users": config.SERVER_MAX_USERS,
        "servers": servers,
        "installs": [admin_ops.public_install(i) for i in admin_ops.list_installs()],
        "install_running": admin_ops.any_install_running(),
    })


async def h_admin_add_server(request: web.Request) -> web.Response:
    """POST /api/v1/admin/servers {name, host, ssh_port, ssh_user, ssh_password} — автоустановка панели по SSH. Только админ."""
    uid = user_id_from_request(request)
    deny = _admin_only(uid)
    if deny:
        return deny
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    name = str(body.get("name") or "").strip()
    host = str(body.get("host") or "").strip()
    ssh_user = str(body.get("ssh_user") or "").strip()
    ssh_password = str(body.get("ssh_password") or "")
    try:
        ssh_port = int(body.get("ssh_port") or 22)
    except (TypeError, ValueError):
        ssh_port = 0
    if not name or not host or not ssh_user or not ssh_password or not (1 <= ssh_port <= 65535):
        return web.json_response(
            {"error": "Заполните все поля: имя, IP, порт SSH (1–65535), логин и пароль."},
            status=400,
        )
    import admin_ops
    rec = admin_ops.start_install(name, host, ssh_port, ssh_user, ssh_password)
    log.info("api-admin tg=%s запустил автоустановку сервера «%s» (%s:%s, user=%s)", uid, name, host, ssh_port, ssh_user)
    return web.json_response({"ok": True, "install": admin_ops.public_install(rec)}, status=202)


async def h_admin_installs(request: web.Request) -> web.Response:
    """GET /api/v1/admin/installs — статусы установок серверов. Только админ."""
    uid = user_id_from_request(request)
    deny = _admin_only(uid)
    if deny:
        return deny
    import admin_ops
    return web.json_response({
        "installs": [admin_ops.public_install(i) for i in admin_ops.list_installs()],
        "install_running": admin_ops.any_install_running(),
    })


# ---------- VK-хеши (звонки) клиентов ----------

async def h_admin_backfill_vk(request: web.Request) -> web.Response:
    """POST /api/v1/admin/backfill-vk — добить VK-хеши до VK_HASHES_PER_USER (4) всем
    активным клиентам, у кого их меньше. Долгая операция — выполняется фоном, ответ 202.
    Только админ."""
    uid = user_id_from_request(request)
    deny = _admin_only(uid)
    if deny:
        return deny

    import bot as b  # lazy: избегаем циклического импорта

    async def _bg() -> None:
        try:
            result = await b.backfill_vk_hashes()
            log.info("api-admin tg=%s: backfill VK-хешей завершён: %s", uid, result)
        except Exception:
            log.exception("api-admin tg=%s: backfill VK-хешей упал", uid)

    asyncio.create_task(_bg())
    log.info("api-admin tg=%s запустил backfill VK-хешей до %s", uid, config.VK_HASHES_PER_USER)
    return web.json_response(
        {"ok": True, "started": True, "target": config.VK_HASHES_PER_USER}, status=202
    )


# ---------- управление администраторами (через админ-панель) ----------

async def h_admin_admins(request: web.Request) -> web.Response:
    """GET /api/v1/admin/admins — список админов (конфиг + назначенные) и инвайт-ссылки. Только админ."""
    uid = user_id_from_request(request)
    deny = _admin_only(uid)
    if deny:
        return deny
    from db import list_admin_invites, list_admins
    db_admins = await list_admins()
    have = {a["telegram_id"] for a in db_admins}
    admins = [
        {
            "telegram_id": a["telegram_id"],
            "source": a["source"],
            "added_by": a.get("added_by"),
            "created_at": a["created_at"],
        }
        for a in db_admins
    ]
    for env_id in sorted(set(config.ADMIN_IDS) - have):
        admins.append({"telegram_id": env_id, "source": "env", "added_by": None, "created_at": 0})
    admins.sort(key=lambda a: a["telegram_id"])
    invites = [
        {
            "code": i["code"],
            "url": bot_url(f"admin_{i['code']}"),
            "used": bool(i["used"]),
            "used_by": i.get("used_by"),
            "created_at": i["created_at"],
        }
        for i in await list_admin_invites()
    ]
    return web.json_response({"admins": admins, "invites": invites})


def _tg_id_from(body: dict) -> int | None:
    raw = body.get("telegram_id")
    if raw is None:
        return None
    try:
        tg = int(raw)
    except (TypeError, ValueError):
        return None
    return tg if tg > 0 else None


async def h_admin_add_admin(request: web.Request) -> web.Response:
    """POST /api/v1/admin/admins {telegram_id} — назначить админа по Telegram ID. Только админ."""
    uid = user_id_from_request(request)
    deny = _admin_only(uid)
    if deny:
        return deny
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    tg = _tg_id_from(body)
    if tg is None:
        return web.json_response({"error": "Нужен положительный telegram_id."}, status=400)
    from db import add_admin
    await add_admin(tg, added_by=uid, source="manual")
    log.info("api-admin tg=%s назначил администратора tg=%s", uid, tg)
    return web.json_response({"ok": True, "telegram_id": tg, "is_admin": True})


async def h_admin_remove_admin(request: web.Request) -> web.Response:
    """POST /api/v1/admin/admins/remove {telegram_id} — снять админа, назначенного через панель. Только админ."""
    uid = user_id_from_request(request)
    deny = _admin_only(uid)
    if deny:
        return deny
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    tg = _tg_id_from(body)
    if tg is None:
        return web.json_response({"error": "Нужен положительный telegram_id."}, status=400)
    if tg in config.ADMIN_IDS:
        return web.json_response(
            {"error": "Этот админ задан в конфиге (ADMIN_IDS) — удалите его там."}, status=400
        )
    if tg == uid:
        return web.json_response({"error": "Нельзя снять самого себя."}, status=400)
    from db import remove_admin
    if not await remove_admin(tg):
        return web.json_response({"error": "Админ не найден."}, status=404)
    log.info("api-admin tg=%s снял администратора tg=%s", uid, tg)
    return web.json_response({"ok": True, "telegram_id": tg})


async def h_admin_create_invite(request: web.Request) -> web.Response:
    """POST /api/v1/admin/invites — создать одноразовую ссылку-приглашение в админы. Только админ."""
    uid = user_id_from_request(request)
    deny = _admin_only(uid)
    if deny:
        return deny
    from db import create_admin_invite
    inv = await create_admin_invite(created_by=uid)
    url = bot_url(f"admin_{inv['code']}")
    log.info("api-admin tg=%s создал инвайт-ссылку в админы %s", uid, inv["code"])
    return web.json_response({"ok": True, "code": inv["code"], "url": url}, status=201)


async def h_admin_revoke_invite(request: web.Request) -> web.Response:
    """POST /api/v1/admin/invites/revoke {code} — отозвать неиспользованное приглашение. Только админ."""
    uid = user_id_from_request(request)
    deny = _admin_only(uid)
    if deny:
        return deny
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    code = str(body.get("code") or "").strip()
    if not code:
        return web.json_response({"error": "Нужен code."}, status=400)
    from db import revoke_admin_invite
    if not await revoke_admin_invite(code):
        return web.json_response({"error": "Приглашение не найдено или уже использовано."}, status=404)
    log.info("api-admin tg=%s отозвал инвайт %s", uid, code)
    return web.json_response({"ok": True, "code": code})


# ---------- настройки экосистемы (ссылка PWDTT для Windows и др.) ----------

def _valid_http_url(url: str) -> bool:
    return (
        0 < len(url) <= 2048
        and (url.startswith("https://") or url.startswith("http://"))
        and " " not in url
    )


async def h_admin_settings_get(request: web.Request) -> web.Response:
    """GET /api/v1/admin/settings — текущие настройки (ссылка PWDTT Windows). Только админ."""
    uid = user_id_from_request(request)
    deny = _admin_only(uid)
    if deny:
        return deny
    from db import get_setting, get_openflux_url, get_openflux_secret
    return web.json_response({
        "app_windows_url": await app_windows_url_effective(),
        "app_windows_url_custom": (await get_setting("app_windows_url")) or "",
        "openflux_url": await get_openflux_url(),
        "openflux_url_custom": (await get_setting("openflux_url")) or "",
        "openflux_secret": await get_openflux_secret(),
        "openflux_secret_custom": (await get_setting("openflux_secret")) or "",
    })


async def h_admin_settings_post(request: web.Request) -> web.Response:
    """POST /api/v1/admin/settings {app_windows_url} — задать ссылку PWDTT (Windows) в подписке/инструкциях. Только админ."""
    uid = user_id_from_request(request)
    deny = _admin_only(uid)
    if deny:
        return deny
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    url = str(body.get("app_windows_url") or "").strip()
    of_url = str(body.get("openflux_url") or "").strip()
    of_secret = str(body.get("openflux_secret") or "").strip()
    from db import conn, set_setting
    if "openflux_url" in body or "openflux_secret" in body:
        if of_url and not _valid_http_url(of_url):
            return web.json_response({"error": "OpenFlux URL должен начинаться с https:// или http:// (без пробелов)."}, status=400)
        if of_url:
            await set_setting("openflux_url", of_url)
        else:
            async with conn() as db:
                await db.execute("DELETE FROM settings WHERE key = 'openflux_url'")
                await db.commit()
        if of_secret:
            await set_setting("openflux_secret", of_secret)
        else:
            async with conn() as db:
                await db.execute("DELETE FROM settings WHERE key = 'openflux_secret'")
                await db.commit()
        log.info("api-admin tg=%s задал OpenFlux url=%s", uid, of_url)
        from db import get_openflux_url, get_openflux_secret
        return web.json_response({
            "ok": True,
            "openflux_url": await get_openflux_url(),
            "openflux_secret": await get_openflux_secret(),
        })
    if not url:
        # пустое значение — сброс на дефолт из конфига
        from db import conn
        async with conn() as db:
            await db.execute("DELETE FROM settings WHERE key = 'app_windows_url'")
            await db.commit()
        log.info("api-admin tg=%s сбросил ссылку PWDTT на дефолт", uid)
        return web.json_response({"ok": True, "app_windows_url": await app_windows_url_effective(), "reset": True})
    if not _valid_http_url(url):
        return web.json_response({"error": "Ссылка должна начинаться с https:// или http:// (без пробелов)."}, status=400)
    from db import set_setting
    await set_setting("app_windows_url", url)
    log.info("api-admin tg=%s задал ссылку PWDTT: %s", uid, url)
    return web.json_response({"ok": True, "app_windows_url": url})


def register_v1_routes(app: web.Application, prefix: str = "/api/v1") -> None:
    """Монтирует единый API экосистемы в aiohttp-приложение (сайт или мини-апп)."""
    app.router.add_post(f"{prefix}/auth", h_auth)
    app.router.add_post(f"{prefix}/auth/email/send-code", h_email_send_code)
    app.router.add_post(f"{prefix}/auth/email/verify", h_email_verify)
    app.router.add_post(f"{prefix}/claim-trial", h_claim_trial)
    app.router.add_get(f"{prefix}/connections", h_connections)
    app.router.add_get(f"{prefix}/block-methods", h_block_methods)
    app.router.add_post(f"{prefix}/block-diagnose", h_block_diagnose)
    app.router.add_post(f"{prefix}/connections/save", h_connections_save)
    app.router.add_get(f"{prefix}/ecosystem", h_ecosystem)
    app.router.add_get(f"{prefix}/plans", h_plans)
    app.router.add_get(f"{prefix}/me", h_me)
    app.router.add_post(f"{prefix}/create-payment", h_create_payment)
    app.router.add_get(f"{prefix}/check-payment", h_check_payment)
    app.router.add_get(f"{prefix}/settings", h_settings_get)
    app.router.add_post(f"{prefix}/settings", h_settings_post)
    app.router.add_get(f"{prefix}/instructions", h_instructions)
    app.router.add_get(f"{prefix}/admin", h_admin_overview)
    app.router.add_post(f"{prefix}/admin/servers", h_admin_add_server)
    app.router.add_get(f"{prefix}/admin/installs", h_admin_installs)
    app.router.add_post(f"{prefix}/admin/backfill-vk", h_admin_backfill_vk)
    app.router.add_get(f"{prefix}/admin/admins", h_admin_admins)
    app.router.add_post(f"{prefix}/admin/admins", h_admin_add_admin)
    app.router.add_post(f"{prefix}/admin/admins/remove", h_admin_remove_admin)
    app.router.add_post(f"{prefix}/admin/invites", h_admin_create_invite)
    app.router.add_post(f"{prefix}/admin/invites/revoke", h_admin_revoke_invite)
    app.router.add_get(f"{prefix}/admin/settings", h_admin_settings_get)
    app.router.add_post(f"{prefix}/admin/settings", h_admin_settings_post)
