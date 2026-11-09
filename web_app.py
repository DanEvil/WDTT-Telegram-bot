"""Веб-сайт продажи подписок WDTT (доступ без VPN).

Работает параллельно с Telegram-ботом, использует ту же SQLite БД,
ту же логику оплаты через ЮКassa и тот же клиент панели WDTT (wdtt.py).

Маршруты:
  GET  /                      — главная: тарифы, вход/регистрация
  GET  /auth/telegram         — приём данных от Telegram Login Widget
  GET  /profile               — профиль: подписка, срок, трафик, ссылка wdtt://
  GET  /buy/<plan_id>         — создать платёж ЮKassa для тарифа
  GET  /pay/success           — возврат после успешной оплаты
  GET  /pay/cancel            — возврат после отмены
  POST /yookassa-webhook      — вебхук ЮKassa (мгновенное уведомление)
  GET  /instruction           — инструкция подключения (Android/iOS)
  GET  /admin/*               — админ-панель (только ADMIN_IDS)
  GET  /logout                — выход

Сессии: простые cookie-сессии (session_id в httpOnly cookie).
Аутентификация: Telegram Login Widget (проверка подписи через бота).
"""

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone

from aiohttp import web

from config import (
    ADMIN_IDS, TELEGRAM_TOKEN, RETURN_URL, DB_PATH,
    YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY,
    WEBHOOK_SKIP_IP_CHECK, SITE_URL,
)
import config
from server_select import pick_least_loaded_server_id
from db import (
    conn as db_conn, init_db, get_customer, upsert_customer,
    add_payment, get_payment, list_payments, get_plan, list_plans,
    claim_trial, list_servers, stats, get_server, server_active_counts,
    get_referrals_count, get_site_user_by_email,
    generate_referral_code, add_site_referral, get_site_referrals_count,
    get_site_user_by_referral_code,
    get_auto_renew_settings, set_auto_renew,
    create_login_request, confirm_login_request, get_login_request, mark_login_request_used,
)
from payments import create_payment, fetch_payment
from plans import PLANS, TRIAL, ALL_PLANS_BY_ID
from wdtt import WdttClient, WdttError

log = logging.getLogger("web")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# ---------- брендинг ----------
BRAND = os.getenv("BRAND_NAME", "K.O. Gateway").strip()
BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "").strip()
APP_ANDROID_URL = os.getenv("APP_ANDROID_URL", "https://github.com/amurcanov/proxy-turn-vk-android/releases/download/v1.2.4/WDTT-universal.apk").strip()
APP_IOS_TESTFLIGHT_URL = os.getenv("APP_IOS_TESTFLIGHT_URL", "https://apps.apple.com/app/testflight/id899247664").strip()
APP_IOS_JOIN_URL = os.getenv("APP_IOS_JOIN_URL", "https://testflight.apple.com/join/ANm6cmDv").strip()
APP_WINDOWS_URL = os.getenv(
    "APP_WINDOWS_URL",
    "https://github.com/ildarmaga/wdtt/releases/download/v1.5.76/pwdtt-client-windows-amd64.exe",
).strip()


def _brand_html() -> str:
    """Бренд в шапке: первое слово обычное, остальное акцентным."""
    parts = BRAND.split(" ", 1)
    if len(parts) == 2:
        return f"{html.escape(parts[0])}<span> {html.escape(parts[1])}</span>"
    return html.escape(BRAND)


def _links_html(links: dict) -> str:
    """Ссылки подключения — так же, как в мини-приложении (по одной на платформу)."""
    labels = (
        ("unified", "🔗 Единая ссылка подписки"),
        ("ios", "🍏 iOS"),
        ("android", "🤖 Android"),
        ("universal", "🔗 Универсальная ссылка"),
    )
    rows = []
    for key, label in labels:
        url = (links or {}).get(key)
        if not url:
            continue
        rows.append(
            f'<h4>{html.escape(label)}</h4>'
            f'<div class="mono"><code>{html.escape(str(url))}</code></div>'
        )
    if not rows:
        return '<p style="color:var(--muted);font-size:.85rem">Ссылка временно недоступна.</p>'
    return "".join(rows)


def _site_return_url() -> str:
    """URL возврата с сайта после оплаты.

    ЮKassa не принимает пустой payment_id в return_url, поэтому идентификатор
    платежа подставляется на странице успеха по последнему платежу пользователя.
    """
    base = SITE_URL or ""
    return f"{base}/pay/success" if base else RETURN_URL


def _sub_id(session: dict) -> int:
    """Идентификатор подписки для пользователя сайта.

    Если Telegram привязан — это его telegram_id, иначе — псевдо-id сайта
    (отрицательный site_user_id). Псевдо-id позволяет покупать подписку без
    привязки Telegram; при привязке запись переносится на реальный telegram_id.
    """
    tg = session.get("telegram_id")
    if tg:
        return int(tg)
    return -abs(int(session.get("site_user_id") or 0))


def _bot_link(start: str = "") -> str:
    """Ссылка на бота в Telegram, опционально с deep-link параметром start."""
    if not BOT_USERNAME:
        return ""
    suffix = f"?start={start}" if start else ""
    return f"https://t.me/{BOT_USERNAME}{suffix}"

# ---------- простые сессии в памяти ----------
SESSION_SECRET = os.getenv("WEB_SESSION_SECRET", os.urandom(24).hex())
SESSIONS: dict[str, dict] = {}  # session_id -> {"site_user_id": int, "email": str, "username": str|None, "telegram_id": int|None, "created": float}
# TTL сессии сайта (по умолчанию 30 дней) и лимит на число живых сессий
SESSION_TTL = int(os.getenv("WEB_SESSION_TTL", str(30 * 86400)))
SESSION_MAX = int(os.getenv("WEB_SESSION_MAX", "10000"))


def _session_expired(sess: dict) -> bool:
    created = float(sess.get("created") or 0)
    return (time.time() - created) > SESSION_TTL


def _prune_sessions() -> None:
    """Выкидывает протухшие сессии и ограничивает размер справедливости."""
    now = time.time()
    expired = [sid for sid, s in SESSIONS.items() if (now - float(s.get("created") or 0)) > SESSION_TTL]
    for sid in expired:
        SESSIONS.pop(sid, None)
    if len(SESSIONS) > SESSION_MAX:
        # удаляем самые старые
        oldest = sorted(SESSIONS.items(), key=lambda kv: float(kv[1].get("created") or 0))
        for sid, _ in oldest[:len(SESSIONS) - SESSION_MAX]:
            SESSIONS.pop(sid, None)


def _get_session(request: web.Request) -> dict | None:
    """Алиас для обратной совместимости — читает текущую сессию сайта."""
    sid = request.cookies.get("session_id")
    if sid and sid in SESSIONS:
        sess = SESSIONS[sid]
        if _session_expired(sess):
            SESSIONS.pop(sid, None)
            return None
        return sess
    return None

# ---------- пароли (site_users) ----------

import re
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def _is_valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email)) and len(email) <= 254

def _hash_password(password: str, salt: str | None = None) -> str:
    if salt is None:
        salt = secrets.token_hex(16)
    pwd_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return salt + ":" + pwd_hash.hex()

def _check_password(password: str, stored: str) -> bool:
    try:
        salt, hex_hash = stored.split(":", 1)
        pwd_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
        return hmac.compare_digest(pwd_hash.hex(), hex_hash)
    except (ValueError, AttributeError):
        return False

# ---------- сессия сайта ----------

_get_site_session = _get_session  # единая реализация сессии

def set_site_session(response: web.Response, site_user: dict) -> None:
    sid = uuid.uuid4().hex
    _prune_sessions()
    SESSIONS[sid] = {
        "site_user_id": site_user["id"],
        "email": site_user["email"],
        "username": site_user.get("username"),
        "telegram_id": site_user.get("telegram_id"),
        "created": time.time(),
    }
    response.set_cookie("session_id", sid, httponly=True, samesite="Lax", path="/")

def _require_site_session(request: web.Request) -> dict:
    s = _get_site_session(request)
    if not s:
        raise web.HTTPFound("/login")
    return s

def _is_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_IDS

# ---------- вспомогательные HTML ----------

_FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E"
    "%3Ccircle cx='50' cy='50' r='48' fill='%23e01e2b'/%3E"
    "%3Ctext x='50' y='66' font-size='40' font-family='Arial,Helvetica,sans-serif' "
    "font-weight='bold' fill='white' text-anchor='middle'%3EK.O.%3C/text%3E"
    "%3C/svg%3E"
)

def _page(title: str, body: str, session: dict | None = None, active: str = "") -> str:
    # Шапка сайта минимальна: только бренд K.O. Gateway и кнопка «Бот в Telegram».
    tg_btn = (
        f"<a class='tg-btn' href='{_bot_link()}' target='_blank' rel='noopener'>"
        f"<svg class='tg-ico' viewBox='0 0 24 24' aria-hidden='true'><path fill='currentColor' d="
        f"\"M21.9 4.3 18.9 19c-.2 1-.8 1.2-1.6.8l-4.4-3.3-2.1 2c-.2.2-.4.4-.9.4l.3-4.5 8.2-7.4c.4-.3-.1-.5-.5-.2L7.9 13.3 3.5 11.8c-1-.3-1-1 .2-1.4l16.6-6.4c.8-.3 1.5.2 1.6 1.3z\"/></svg>"
        f"<span>Бот в Telegram</span></a>"
    ) if BOT_USERNAME else ""
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0a0a0c">
<meta name="description" content="{html.escape(BRAND)} — управление подпиской, подключение и оплата">
<meta property="og:title" content="{html.escape(title)} — {html.escape(BRAND)}">
<meta property="og:description" content="Подписка и подключение без VPN: Android, iOS, Windows">
<meta property="og:type" content="website">
<link rel="icon" href="{_FAVICON}">
<link rel="stylesheet" href="/static/site.css">
<title>K.O.Gateway</title>
</head>
<body>
<a class="skip-link" href="#main">Перейти к содержимому</a>
<header class="topbar">
  <div class="brand">{_brand_html()}</div>
  <div class="topbar-right">{tg_btn}</div>
</header>
<main id="main">
<h1 class="page-title">{html.escape(title)}</h1>
{body}
</main>
<script src="/static/site.js"></script>
</body>
</html>"""

def _csrf_check_telegram(data: dict) -> bool:
    """Проверка подписи Telegram Login Widget."""
    if not TELEGRAM_TOKEN:
        return False
    check_hash = data.pop("hash", None)
    if not check_hash:
        return False
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()) if v is not None)
    secret_key = hashlib.sha256(TELEGRAM_TOKEN.encode()).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(calc_hash, check_hash)

# ---------- маршруты ----------

async def handle_index(request: web.Request) -> web.Response:
    session = _get_session(request)
    plans = await list_plans() or list(PLANS)
    plans_html = '<div class="plans">'
    for p in plans:
        plans_html += f"""
        <div class="plan">
          <h3>{html.escape(p['name'])}</h3>
          <p class="desc">{html.escape(p.get('description',''))}</p>
          <div class="price">{fmt_price(p['price'])} ₽</div>
          <p class="meta">{p['days']} дн. · {'безлимит' if not p.get('traffic_gb') else str(p['traffic_gb']) + ' ГБ'} · до {p.get('max_devices',1)} устр.</p>
          <a href="/buy/{p['id']}" class="btn">Купить</a>
        </div>"""
    plans_html += "</div>"
    trial_html = ""
    login_cta = ""
    if session:
        if not await has_used_trial_web(_sub_id(session)):
            trial_html = f"""
                <div class="card trial-card">
                  <h3>🎁 Пробный период</h3>
                  <p>{TRIAL['days']} дн. · Трафик: {fmt_traffic(TRIAL['traffic_gb'])} · Устройств: {TRIAL['max_devices']}</p>
                  <a href="/buy/trial" class="btn secondary">Активировать пробный период</a>
                </div>
                """
    else:
        # для гостей — заметная кнопка входа через Telegram
        login_cta = """
    <div class="card tg-cta">
      <h3>🔐 Вход через Telegram</h3>
      <p>Войдите одним нажатием — без пароля. Подписка, ссылки для подключения
         и реферальная программа сразу появятся в профиле.</p>
      <a href="/login/telegram" class="btn">Войти через Telegram</a>
      <a href="/login" class="btn secondary">Войти по email</a>
    </div>
        """ if BOT_USERNAME else ""
    apps_html = f"""
    <div class="card apps-card">
      <h3>📱 Приложения для подключения</h3>
      <div class="apps">
        <a class="app-link" href="{APP_ANDROID_URL}" target="_blank" rel="noopener">
          <span class="app-ico">🤖</span><span>Android<br><small>скачать APK</small></span>
        </a>
        <a class="app-link" href="{APP_IOS_TESTFLIGHT_URL}" target="_blank" rel="noopener">
          <span class="app-ico">🍎</span><span>iOS<br><small>TestFlight → <a href="{APP_IOS_JOIN_URL}" target="_blank" rel="noopener">приглашение</a></small></span>
        </a>
        <a class="app-link" href="{APP_WINDOWS_URL}" target="_blank" rel="noopener">
          <span class="app-ico">🪟</span><span>Windows<br><small>скачать PWDTT</small></span>
        </a>
      </div>
      <p style="color:var(--muted);font-size:.85rem;margin-bottom:0">
        Пошаговая инструкция со скриншотами — в <a href="/instruction">разделе «Инструкция»</a> и в нашем
        <a href="{_bot_link() or '#'}">боте Telegram</a>.
      </p>
    </div>
    """
    eco_html = ""
    body = f"""
    {login_cta}
    {trial_html}
    <div id="plans"></div>
    <h2>Тарифы</h2>
    {plans_html or "<p>Тарифы не настроены.</p>"}
    {apps_html}
    """
    return web.Response(text=_page("Главная", body, session, active="Главная"), content_type="text/html")

# ---------- регистрация / вход на сайт ----------

_REGISTER_FORM_TG = """
<div class="card" style="max-width:400px;margin:2rem auto">
  <h3>Регистрация</h3>
  <form method="post" action="/register">
    <label>Email
      <input type="email" name="email" required placeholder="you@example.com">
    </label>
    <label>Пароль
      <input type="password" name="password" required minlength="6" placeholder="минимум 6 символов">
    </label>
    <label>Имя (необязательно)
      <input type="text" name="username" placeholder="отображается в профиле">
    </label>
    <button class="btn" type="submit" style="width:100%">Создать аккаунт</button>
  </form>
  {tg_block}
  <p style="text-align:center;margin-top:1rem;color:var(--muted);font-size:.9rem">
    Уже есть аккаунт? <a href="/login">Войти</a>
  </p>
</div>
"""

def _tg_login_block(request) -> str:
    """Кнопка входа через Telegram: открывает бота с одноразовым кодом.

    Login Widget требует привязки домена к боту в @BotFather, поэтому рабочий
    путь — deep-link в бота, где пользователь подтверждает вход.
    """
    if not BOT_USERNAME:
        return ""
    return """
  <div class="tg-auth">
    <div class="sep"><span>или войти через Telegram</span></div>
    <p style="text-align:center;margin:0">
      <a class="btn secondary" href="/login/telegram">🔐 Войти через Telegram</a>
    </p>
  </div>"""


def _login_form(request) -> str:
    return f"""
<div class="card" style="max-width:400px;margin:2rem auto">
  <h3>Вход</h3>
  <form method="post" action="/login">
    <label>Email
      <input type="email" name="email" required placeholder="you@example.com">
    </label>
    <label>Пароль
      <input type="password" name="password" required>
    </label>
    <button class="btn" type="submit" style="width:100%">Войти</button>
  </form>
  {_tg_login_block(request)}
  <p style="text-align:center;margin-top:1rem;color:var(--muted);font-size:.9rem">
    Нет аккаунта? <a href="/register">Зарегистрироваться</a>
  </p>
</div>
"""

_BIND_FORM = """
<div class="card" style="max-width:480px;margin:2rem auto">
  <h3>🔗 Привязка Telegram</h3>
  <p>Нажмите кнопку ниже — откроется наш бот в Telegram, и аккаунт на сайте
     автоматически привяжется к вашему профилю (данные запишутся в базу).</p>
  <p style="text-align:center;margin:1.2rem 0">
    <a class="btn" href="{deep_link}" target="_blank" rel="noopener">🤖 Открыть бота и привязать Telegram</a>
  </p>
  <p style="color:var(--muted);font-size:.85rem">Или вручную: отправьте боту команду</p>
  <div class="mono"><code>/bind {code}</code></div>
  <p style="color:var(--muted);font-size:.85rem">После привязки статус подписки будет виден и на сайте, и в боте.</p>
  <a href="/profile" class="btn secondary">В профиль</a>
</div>
"""

async def handle_register_get(request: web.Request) -> web.Response:
    session = _get_session(request)
    if session:
        raise web.HTTPFound("/profile")
    return web.Response(text=_page("Регистрация", _REGISTER_FORM_TG.replace("{tg_block}", _tg_login_block(request)), session), content_type="text/html")

async def handle_register_post(request: web.Request) -> web.Response:
    data = await request.post()
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")
    username = str(data.get("username") or "").strip() or email.split("@")[0]
    if not email or not password:
        return web.Response(text=_page("Ошибка", "<p class='err'>Укажите email и пароль.</p><a href='/register'>Назад</a>"), content_type="text/html")
    if not _is_valid_email(email):
        return web.Response(text=_page("Ошибка", "<p class='err'>Некорректный email.</p><a href='/register'>Назад</a>"), content_type="text/html")
    await init_db()
    existing = await get_site_user_by_email(email)
    if existing:
        return web.Response(text=_page("Ошибка", "<p class='err'>Пользователь с таким email уже существует.</p><a href='/login'>Войти</a>"), content_type="text/html")
    referral_code = generate_referral_code()
    async with db_conn() as db:
        cur = await db.execute(
            "INSERT INTO site_users (email, username, password_hash, referral_code, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (email, username, _hash_password(password), referral_code, int(time.time())),
        )
        site_user_id = cur.lastrowid
    # учитываем реф-приглашение, если приходил по реферальной ссылке
    ref_code = request.cookies.get("ref_code")
    if ref_code:
        referrer = await get_site_user_by_referral_code(ref_code)
        if referrer and referrer["id"] != site_user_id:
            await add_site_referral(site_user_id, referrer["id"])
    site_user = {"id": site_user_id, "email": email, "username": username, "telegram_id": None, "referral_code": referral_code}
    response = web.HTTPFound("/profile")
    set_site_session(response, site_user)
    response.del_cookie("ref_code", path="/")
    return response

async def handle_login_get(request: web.Request) -> web.Response:
    session = _get_session(request)
    if session:
        raise web.HTTPFound("/profile")
    return web.Response(text=_page("Вход", _login_form(request), session), content_type="text/html")

async def handle_login_post(request: web.Request) -> web.Response:
    data = await request.post()
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")
    if not email or not password:
        return web.Response(text=_page("Ошибка", "<p class='err'>Укажите email и пароль.</p><a href='/login'>Назад</a>"), content_type="text/html")
    if not _is_valid_email(email):
        return web.Response(text=_page("Ошибка", "<p class='err'>Некорректный email.</p><a href='/login'>Назад</a>"), content_type="text/html")
    await init_db()
    site_user = await get_site_user_by_email(email)
    if not site_user or not _check_password(password, site_user["password_hash"]):
        return web.Response(text=_page("Ошибка", "<p class='err'>Неверный email или пароль.</p><a href='/login'>Попробовать снова</a>"), content_type="text/html")
    response = web.HTTPFound("/profile")
    set_site_session(response, site_user)
    return response

async def handle_bind_telegram(request: web.Request) -> web.Response:
    session = _require_site_session(request)
    await init_db()
    # создаём код привязки, если ещё не создан
    async with db_conn() as db:
        cur = await db.execute("SELECT code FROM bind_codes WHERE site_user_id = ? AND used = 0", (session["site_user_id"],))
        row = await cur.fetchone()
        if not row:
            code = secrets.token_urlsafe(8)
            await db.execute(
                "INSERT INTO bind_codes (code, site_user_id, created_at, used) VALUES (?, ?, ?, 0)",
                (code, session["site_user_id"], int(time.time())),
            )
        else:
            code = row["code"]
    deep_link = _bot_link(f"bind_{code}")
    body = _BIND_FORM.format(code=code, deep_link=deep_link)
    return web.Response(text=_page("Привязка Telegram", body, session), content_type="text/html")

async def handle_login_telegram(request: web.Request) -> web.Response:
    """Начало входа через Telegram: создаём код и отправляем пользователя в бота."""
    if not BOT_USERNAME:
        raise web.HTTPNotFound()
    await init_db()
    code = secrets.token_urlsafe(24)
    await create_login_request(code)
    raise web.HTTPFound(f"https://t.me/{BOT_USERNAME}?start=web_login_{code}")


async def _telegram_login_by_code(code: str, max_age: int) -> web.Response:
    """Вход на сайт по одноразовому коду из login_requests (telegram_id уже записан).

    max_age — срок жизни ссылки в секундах (для автовхода из бота — 10 минут).
    """
    await init_db()
    row = await get_login_request(code)
    if (not row or not row.get("telegram_id") or row.get("used")
            or int(row.get("created_at") or 0) < int(time.time()) - max_age):
        return web.Response(
            text=_page("Ошибка", "<p class='err'>Ссылка входа недействительна или уже использована.</p>"
                              "<p><a href='/login'>Войти заново</a></p>"),
            status=400,
        )
    telegram_id = int(row["telegram_id"])
    username = row.get("username") or None

    async with db_conn() as db:
        cur = await db.execute("SELECT * FROM site_users WHERE telegram_id = ?", (telegram_id,))
        site_user = await cur.fetchone()
        if site_user:
            site_user_id = site_user["id"]
            user_name = site_user["username"]
        else:
            referral_code = generate_referral_code()
            cur = await db.execute(
                "INSERT INTO site_users (email, username, password_hash, telegram_id, referral_code, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (f"tg_{telegram_id}@wdtt.local", username or str(telegram_id), "",
                 telegram_id, referral_code, int(time.time())),
            )
            site_user_id = cur.lastrowid
            user_name = username or str(telegram_id)
        await db.execute(
            "INSERT INTO customers (telegram_id, username, expires_at, plan_id, server_id, created_at) "
            "VALUES (?, ?, 0, NULL, NULL, ?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET username=COALESCE(excluded.username, customers.username)",
            (telegram_id, username or "", int(time.time())),
        )

    await mark_login_request_used(code)
    response = web.HTTPFound("/profile")
    set_site_session(response, {
        "id": site_user_id,
        "email": None,
        "username": user_name,
        "telegram_id": telegram_id,
    })
    return response


async def handle_login_confirm(request: web.Request) -> web.Response:
    """Подтверждение входа: бот записал telegram_id — выдаём сессию сайта."""
    code = (request.query.get("code") or "").strip()
    return await _telegram_login_by_code(code, max_age=3600)


async def handle_login_auto(request: web.Request) -> web.Response:
    """Автовход на сайт по ссылке из бота (t.me/... -> сайт): сессия сразу, без кликов.

    Бот генерирует одноразовый код с уже записанным telegram_id —
    переход по ссылке «Сайт» в боте сразу открывает профиль на сайте.
    """
    code = (request.query.get("code") or "").strip()
    return await _telegram_login_by_code(code, max_age=600)


async def handle_auth_telegram(request: web.Request) -> web.Response:
    """Приём данных от Telegram Login Widget."""
    data = dict(request.query)
    if not _csrf_check_telegram(data):
        return web.Response(text=_page("Ошибка", "<p class='err'>Не удалось подтвердить данные от Telegram.</p>"), status=403)

    telegram_id = int(data["id"])
    username = data.get("username")
    first_name = data.get("first_name", "")

    await init_db()
    # создаём/обновляем покупателя в customers (тг-пользователь)
    async with db_conn() as db:
        await db.execute(
            "INSERT INTO customers (telegram_id, username, expires_at, plan_id, server_id, created_at) "
            "VALUES (?, ?, 0, NULL, NULL, ?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET username=COALESCE(excluded.username, customers.username)",
            (telegram_id, username or first_name or "", int(time.time())),
        )
        # ищем или создаём site_user, привязанный к этому telegram_id
        cur = await db.execute("SELECT * FROM site_users WHERE telegram_id = ?", (telegram_id,))
        site_user = await cur.fetchone()
        if not site_user:
            # создаём site_user без пароля — пользователь сможет задать пароль позже
            referral_code = generate_referral_code()
            cur = await db.execute(
                "INSERT INTO site_users (email, username, password_hash, telegram_id, referral_code, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (f"tg_{telegram_id}@wdtt.local", username or first_name or str(telegram_id), "", telegram_id, referral_code, int(time.time())),
            )
            site_user_id = cur.lastrowid
        else:
            site_user_id = site_user["id"]

    response = web.HTTPFound("/profile")
    set_site_session(response, {
        "id": site_user_id,
        "email": None,
        "username": username or first_name or str(telegram_id),
        "telegram_id": telegram_id,
    })
    return response

async def _ensure_bind_code(site_user_id: int) -> str:
    """Возвращает неиспользованный код привязки, создавая его при необходимости."""
    async with db_conn() as db:
        cur = await db.execute(
            "SELECT code FROM bind_codes WHERE site_user_id = ? AND used = 0 ORDER BY created_at DESC LIMIT 1",
            (site_user_id,),
        )
        row = await cur.fetchone()
        if row:
            return row["code"]
        code = secrets.token_urlsafe(8)
        await db.execute(
            "INSERT INTO bind_codes (code, site_user_id, created_at, used) VALUES (?, ?, ?, 0)",
            (code, site_user_id, int(time.time())),
        )
        return code


async def handle_profile(request: web.Request) -> web.Response:
    session = _require_site_session(request)
    site_user_id = session["site_user_id"]
    tg_id = session.get("telegram_id")
    await init_db()

    # загружаем site_user для referral_code
    async with db_conn() as db:
        cur = await db.execute("SELECT * FROM site_users WHERE id = ?", (site_user_id,))
        row = await cur.fetchone()
        site_user = dict(row) if row else {}

    customer = await get_customer(_sub_id(session))
    now = int(time.time())

    # ----- реферальные ссылки -----
    bot_username = (os.getenv("TELEGRAM_BOT_USERNAME") or "").strip()
    tg_ref_link = f"https://t.me/{bot_username}?start=ref_{tg_id}" if (bot_username and tg_id) else "—"
    site_ref_link = f"{SITE_URL}/r/{site_user['referral_code']}" if (site_user.get("referral_code") and SITE_URL) else "—"
    tg_ref_count = await get_referrals_count(tg_id) if tg_id else 0
    site_ref_count = await get_site_referrals_count(site_user_id)

    # ----- подписка -----
    body = ""
    if not customer or not customer.get("wdtt_password"):
        body += """
        <div class="card">
          <h3>У вас нет активной подписки</h3>
          <p>Перейдите на <a href="/">Главную</a>, чтобы выбрать тариф и оплатить.</p>
        </div>
        """
    else:
        expires_at = int(customer["expires_at"] or 0)
        active = expires_at > now
        plan = await get_plan(customer["plan_id"]) or ALL_PLANS_BY_ID.get(customer["plan_id"])
        plan_name = plan["name"] if plan else (customer["plan_id"] or "—")
        diff = expires_at - now
        days, hours = diff // 86400, (diff % 86400) // 3600

        # ссылки подключения и трафик из панели (как в мини-приложении)
        links: dict = {}
        used_bytes = 0
        total_gb = int(plan.get("traffic_gb") or 0) if plan else 0
        try:
            sid = customer["server_id"] or await pick_least_loaded_server_id()
            if sid:
                server = await get_server(sid)
                client = WdttClient(server["base_url"], server["username"], server["password"], verify_ssl=bool(server["verify_ssl"]))
                await client.login()
                import bot as _bot  # lazy: bot импортирует web_app
                links = await _bot.links_json(client, customer["wdtt_password"], customer.get("sub_id"))
                info = await client.get_user_info(customer["wdtt_password"]) or {}
                used_bytes = int(info.get("up_bytes") or 0) + int(info.get("down_bytes") or 0)
                await client.close()
        except Exception as e:
            log.warning("не удалось получить ссылки подключения: %s", e)
        links_html = _links_html(links)

        # прогресс-бар трафика
        traffic_html = ""
        if plan:
            if total_gb:
                pct = min(100, used_bytes / (total_gb * 1024**3) * 100) if total_gb else 0
                traffic_html = f"""
          <p>📶 Трафик: <b>{fmt_bytes(used_bytes)}</b> из {fmt_traffic(total_gb)}</p>
          <div class="progress"><div style="width:{pct:.1f}%"></div></div>"""
            else:
                traffic_html = f"""
          <p>📶 Трафик: <b>{fmt_bytes(used_bytes)}</b> (безлимит)</p>"""
            traffic_html += f"""
          <p>Устройств: до {plan.get('max_devices', 1)}</p>"""

        badge = "ok" if active else "err"
        status_text = "Активна" if active else "Истекла"
        body += f"""
        <div class="card">
          <h3>📊 Ваша подписка</h3>
          <p>Тариф: <b>{html.escape(str(plan_name))}</b></p>
          <p>Действует до: <b>{fmt_date(expires_at)}</b> (осталось {days} дн. {hours} ч)</p>
          <p>Статус: <span class="badge {badge}">{status_text}</span></p>
          {traffic_html}
          <h4>🔗 Ссылка для подключения:</h4>
          {links_html}
          <p style="color:var(--muted);font-size:.85rem">Скопируйте ссылку в приложение WDTT (Android / iOS / Windows).</p>
          <a href="{APP_WINDOWS_URL}" class="btn secondary" target="_blank" rel="noopener">🪟 Подключить на Windows — скачать PWDTT</a>
          <a href="/buy/{customer['plan_id']}" class="btn secondary">Продлить подписку</a>
        </div>
        """

    # ----- автопродление (только при привязанном Telegram) -----
    if tg_id:
        ar = await get_auto_renew_settings(tg_id)
        month_plan = await get_plan("month") or ALL_PLANS_BY_ID.get("month") or {}
        month_price = fmt_price(month_plan.get("price") or 0)
        notify_opts = "".join(
            f"<option value='{h}'{' selected' if h == ar['notify_hours'] else ''}>"
            f"{'Не уведомлять' if h == 0 else f'За {h} ч.'}</option>"
            for h in (0, 1, 3, 6, 12, 24, 48)
        )
        # магазин ЮKassa без рекуррентов -> карта не сохранится, автопродление невозможно
        from payments import recurring_supported
        no_recurring = recurring_supported() is False
        renew_note = (
            "<p style='color:var(--err);font-size:.85rem'>⚠️ Автопродление сейчас недоступно: "
            "платёжный шлюз не поддерживает повторные списания для этого магазина, карта не сохраняется. "
            "Оплата и продление вручную работают.</p>"
            if no_recurring else
            ("<p style='color:var(--warn);font-size:.85rem'>⚠️ Сначала оплатите любой тариф картой через ЮKassa — карта сохранится для автопродления.</p>"
             if (ar['auto_renew'] and not ar['has_payment_method']) else '')
        )
        body += f"""
    <div class="card" id="settings">
      <h3>🔄 Автопродление подписки</h3>
      <form method="post" action="/settings">
        <label class="check">
          <input type="checkbox" name="auto_renew" value="1"{' checked' if ar['auto_renew'] else ''}{' disabled' if no_recurring else ''}>
          <span>Автоматически продлевать подписку на <b>1 месяц (30 дней)</b> —
            при окончании спишем <b>{month_price} ₽</b> с сохранённой карты ЮKassa.</span>
        </label>
        {renew_note}
        <h4 style="margin-top:1rem">🔔 Уведомление о списании</h4>
        <label class="check">
          <input type="checkbox" id="notify_on" name="notify_on" value="1"{' checked' if ar['notify_hours'] > 0 else ''}
                 onchange="document.getElementById('notify_hours').disabled = !this.checked">
          <span>Уведомлять меня о предстоящем списании средств</span>
        </label>
        <select id="notify_hours" name="notify_hours" style="margin-top:.5rem;max-width:280px"{' disabled' if ar['notify_hours'] == 0 else ''}>
          {notify_opts}
        </select>
        <p style="margin-top:1rem"><button class="btn" type="submit">Сохранить настройки</button></p>
      </form>
      <p style="color:var(--muted);font-size:.82rem">Автопродление всегда добавляет только 30 дней по цене месячного тарифа. Отключить можно в любой момент.</p>
    </div>
        """

    # ----- привязка Telegram -----
    if not tg_id:
        bind_link = _bot_link(f"bind_{await _ensure_bind_code(site_user_id)}")
        body += f"""
    <div class="card">
      <h3>🔗 Привязка Telegram</h3>
      <p>Привяжите свой Telegram-аккаунт, чтобы видеть подписку и в боте.</p>
      {f'<p style="color:var(--muted);font-size:.85rem">Нажмите кнопку — откроется Telegram, и аккаунт привяжется автоматически.</p><a href="{bind_link}" class="btn btn-tg" target="_blank" rel="noopener">Перейти в Telegram и привязать</a>' if bind_link else '<a href="/bind-telegram" class="btn">Получить код привязки</a>'}
    </div>
        """

    # ----- рефералы -----
    tg_ref_hint = (
        ""
        if tg_id
        else '<p style="color:var(--muted);font-size:.85rem">Для начала необходимо подключить Telegram-аккаунт</p>'
    )
    body += f"""
    <div class="card">
      <h3>👥 Реферальная программа</h3>
      <p><b>Ссылка в Telegram:</b></p>
      <div class="mono"><code>{html.escape(tg_ref_link)}</code></div>
      {tg_ref_hint}
      <p style="color:var(--muted);font-size:.85rem">Приглашено через TG: <b>{tg_ref_count}</b></p>
      <p><b>Ссылка на сайт:</b></p>
      <div class="mono"><code>{html.escape(site_ref_link)}</code></div>
      <p style="color:var(--muted);font-size:.85rem">Приглашено через сайт: <b>{site_ref_count}</b></p>
      <p style="color:var(--muted);font-size:.85rem">После первой оплаты приглашённого вы получаете бонусные дни к подписке.</p>
    </div>
    """

    # ----- история платежей -----
    # Убрано по требованию: в профиле на сайте платёжная история не показывается.

    return web.Response(text=_page("Профиль", body, session), content_type="text/html")

async def handle_buy(request: web.Request) -> web.Response:
    session = _require_site_session(request)
    sub_id = _sub_id(session)
    plan_id = request.match_info.get("plan_id")
    await init_db()
    plan = await get_plan(plan_id) or ALL_PLANS_BY_ID.get(plan_id)
    if not plan:
        return web.Response(text=_page("Ошибка", "<p class='err'>Тариф не найден.</p>"), status=404)

    if plan_id == "trial":
        if not await claim_trial(sub_id):
            return web.Response(text=_page("Ошибка", "<p class='err'>Вы уже использовали пробный период.</p>"))
        # пробный период — выдаём сразу без оплаты, на наименее загруженный сервер
        try:
            sid = await pick_least_loaded_server_id()
            if not sid:
                return web.Response(text=_page("Ошибка", "<p class='err'>Нет свободных серверов WDTT (все заполнены до лимита). Обратитесь к администратору.</p>"))
            server = await get_server(sid)
            client = WdttClient(server["base_url"], server["username"], server["password"], verify_ssl=bool(server["verify_ssl"]))
            await client.login()
            now = int(time.time())
            expires = now + int(plan["days"]) * 86400
            pwd = await client.add_user(
                comment=f"web:{sub_id}",
                expires_at=expires,
                total_gb=int(plan.get("traffic_gb") or 0),
                max_devices=int(plan.get("max_devices") or 1),
                vk_hash="",
            )
            await _ensure_vk_hash(client, pwd)
            await upsert_customer(sub_id, pwd, expires, plan["id"], sid,
                                  username=session.get("username"))
            link = await client.find_link(pwd)
            await client.close()
            return web.Response(text=_page("Пробный период", f"""
            <div class="card ok">
              <h3>🎁 Пробный период активирован!</h3>
              <p>Срок: {plan['days']} дн. · Трафик: {fmt_traffic(plan.get('traffic_gb'))} · Устройств: {plan.get('max_devices')}</p>
              <h4>🔗 Ссылка:</h4><div class="mono">{html.escape(str(link))}</div>
              <p>Инструкция: <a href="/instruction">здесь</a>.</p>
            </div>
            <a href="/profile">В профиль</a>
            """, session), content_type="text/html")
        except Exception as e:
            log.exception("ошибка выдачи пробного периода через веб")
            return web.Response(text=_page("Ошибка", f"<p class='err'>Не удалось создать пробный период: {html.escape(str(e))}</p>"))

    # платный тариф — создаём платёж ЮKassa и сразу отправляем на страницу оплаты
    try:
        payment = await create_payment(plan, sub_id, return_url=_site_return_url())
    except Exception as e:
        log.exception("ошибка создания платежа")
        return web.Response(text=_page("Ошибка", f"<p class='err'>Не удалось создать платёж: {html.escape(str(e))}</p>"))

    if not payment.get("url"):
        return web.Response(text=_page("Ошибка", "<p class='err'>ЮKassa не вернула ссылку на оплату.</p>"))

    await add_payment(
        telegram_id=sub_id,
        username=session.get("username"),
        payment_id=payment["id"],
        plan_id=str(plan["id"]),
        amount=f"{float(plan['price']):.2f}",
    )
    raise web.HTTPFound(payment["url"])

async def handle_pay_success(request: web.Request) -> web.Response:
    session = _get_session(request)
    payment_id = request.query.get("payment_id")
    body = "<h2>Оплата</h2>"
    if session and not payment_id:
        # ЮKassa не возвращает payment_id — берём последний платёж пользователя
        try:
            rows = await list_payments(_sub_id(session), limit=1)
            if rows:
                payment_id = rows[0].get("payment_id")
        except Exception:
            log.warning("не удалось получить последний платёж пользователя", exc_info=True)
    if payment_id and session:
        await init_db()
        row = await get_payment(payment_id)
        # платёж должен принадлежать текущему пользователю (иначе можно
        # «сжечь» чужой платёж, не выдав по нему доступ)
        if row and int(row.get("telegram_id") or 0) == _sub_id(session):
            try:
                # ВАЖНО: не помечаем платёж fulfilled вручную — статус
                # выставляет fulfill_payment после фактической выдачи доступа
                from bot import fulfill_payment
                await fulfill_payment(payment_id)
                row = await get_payment(payment_id) or row
                if row.get("status") == "fulfilled":
                    body += "<p class='ok'>Оплата подтверждена. Подписка активирована (или продлена).</p>"
                else:
                    body += "<p>Оплата ещё не поступила. Проверьте статус через минуту.</p>"
            except Exception as e:
                body += f"<p class='err'>Не удалось проверить платёж: {html.escape(str(e))}</p>"
    body += "<a href='/profile'>В профиль</a> | <a href='/'>На главную</a>"
    return web.Response(text=_page("Оплата", body, session), content_type="text/html")

async def handle_pay_cancel(request: web.Request) -> web.Response:
    session = _get_session(request)
    body = "<h2>Оплата отменена</h2><p>Вы можете попробовать ещё раз на <a href='/'>главной странице</a>.</p>"
    return web.Response(text=_page("Отмена", body, session), content_type="text/html")


# ---------- вебхук ЮKassa: защита ----------
# id платежа ЮKassa: uuid-подобная строка
_YOOKASSA_PAYMENT_ID_RE = re.compile(r"^[0-9a-f][0-9a-f-]{15,63}$", re.IGNORECASE)
_site_webhook_seen: deque[str] = deque(maxlen=256)          # дедупликация платежей
_site_webhook_tasks: set[asyncio.Task] = set()               # ссылки на задачи (иначе GC)
_site_webhook_hits: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=30))
_SITE_WEBHOOK_WINDOW = 60.0
_SITE_WEBHOOK_MAX = 30


def _site_webhook_rate_ok(peer: str) -> bool:
    """Грубый rate-limit на один IP для вебхука."""
    now = time.monotonic()
    hits = _site_webhook_hits[peer]
    while hits and now - hits[0] > _SITE_WEBHOOK_WINDOW:
        hits.popleft()
    if len(hits) >= _SITE_WEBHOOK_MAX:
        return False
    hits.append(now)
    return True


async def handle_yookassa_webhook(request: web.Request) -> web.Response:
    """Вебхук ЮKassa — триггер для fulfillment (проверяем через API).

    Защита: IP-белый список ЮKassa, rate-limit на IP, валидация формата
    payment_id и дедупликация — те же правила, что в webhook.py.
    """
    peer = request.remote or "?"
    if not WEBHOOK_SKIP_IP_CHECK:
        from webhook import is_yookassa_ip
        if not is_yookassa_ip(peer):
            log.warning("site webhook: отклонён запрос с не-ЮKassa IP %s", peer)
            return web.Response(status=403, text="forbidden")
    if not _site_webhook_rate_ok(peer):
        log.warning("site webhook: превышен частотный лимит с %s", peer)
        return web.Response(status=429, text="too many requests")
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="bad json")
    if not isinstance(data, dict):
        return web.Response(status=400, text="bad payload")
    if data.get("type") != "notification" or data.get("event") != "payment.succeeded":
        return web.Response(text="ok")
    obj = data.get("object") or {}
    payment_id = obj.get("id")
    if not isinstance(payment_id, str) or not _YOOKASSA_PAYMENT_ID_RE.match(payment_id):
        log.warning("site webhook: некорректный payment_id с %s", peer)
        return web.Response(text="ok")
    if payment_id in _site_webhook_seen:
        return web.Response(text="ok")  # дубль
    _site_webhook_seen.append(payment_id)
    # перепроверяем через API и выполняем fulfillment
    try:
        from bot import fulfill_payment
        task = asyncio.create_task(fulfill_payment(str(payment_id)))
        _site_webhook_tasks.add(task)
        task.add_done_callback(_site_webhook_tasks.discard)
    except Exception as e:
        log.exception("webhook: не удалось запустить fulfilment: %s", e)
    return web.Response(text="ok")

async def handle_instruction(request: web.Request) -> web.Response:
    import instructions as instr

    def platform_html(key: str) -> str:
        p = instr.PLATFORMS[key]
        links = ""
        if p.get("app_url"):
            links += (f"<p><a class='btn' href='{p['app_url']}' target='_blank' rel='noopener'>"
                      f"{html.escape(p['app_label'])}</a></p>")
        for url, label in p.get("links", []):
            links += f"<p><a href='{url}' target='_blank' rel='noopener'>{html.escape(label)}</a></p>"
        steps = "".join(
            f"<figure class='instr-step'><img src='{instr.step_img(img)}' alt='{html.escape(cap)}' loading='lazy'>"
            f"<figcaption>{html.escape(cap)}</figcaption></figure>"
            for img, cap in p["steps"]
        )
        return f"<div class='card'><h3>{p['title']}</h3><p>{p['intro']}</p>{links}{steps}</div>"

    body = f"""
    <h2>📖 Инструкция подключения</h2>
    <div class="card">
      <h3>1. Скачайте приложение</h3>
      <div class="apps">
        <a class="app-link" href="{APP_ANDROID_URL}" target="_blank" rel="noopener">
          <span class="app-ico">🤖</span><span>Android<br><small>APK</small></span>
        </a>
        <a class="app-link" href="{APP_IOS_TESTFLIGHT_URL}" target="_blank" rel="noopener">
          <span class="app-ico">🍎</span><span>iOS<br><small>TestFlight → <a href="{APP_IOS_JOIN_URL}" target="_blank" rel="noopener">приглашение</a></small></span>
        </a>
        <a class="app-link" href="{APP_WINDOWS_URL}" target="_blank" rel="noopener">
          <span class="app-ico">🪟</span><span>Windows<br><small>qWDTT</small></span>
        </a>
      </div>
    </div>
    <div class="card">
      <h3>2. Добавьте конфигурацию</h3>
      <p>Скопируйте вашу ссылку <code>wdtt://...</code> из профиля (в приложении или в боте Telegram),
        и следуйте пошаговой инструкции для вашей платформы ниже.</p>
    </div>
    {platform_html("android")}
    {platform_html("ios")}
    <div class="card">
      <h3>3. Подключитесь</h3>
      <p>Нажмите «Подключить». Готово — весь трафик идёт через защищённый туннель.</p>
    </div>
    """
    return web.Response(text=_page("Инструкция", body, _get_session(request), active="Инструкция"), content_type="text/html")

# ---------- админ ----------

async def handle_admin(request: web.Request) -> web.Response:
    session = _require_site_session(request)
    tg_id = session.get("telegram_id")
    if not tg_id or not _is_admin(tg_id):
        return web.Response(text=_page("Ошибка", "<p class='err'>Доступ только для администратора.</p>"), status=403)
    await init_db()
    s = await stats()
    servers = await list_servers()
    counts = await server_active_counts()
    installs_rows = "".join(
        f"<tr><td>#{i['id']}</td><td>{html.escape(i['name'])}</td><td>{html.escape(i['host'])}</td>"
        f"<td>{'⏳ выполняется' if i['status'] == 'running' else ('✅ готово' if i['status'] == 'done' else '⚠️ ошибка')}</td>"
        f"<td>{html.escape(i.get('detail') or '')}</td></tr>"
        for i in sorted(_installs.values(), key=lambda x: x["id"], reverse=True)[:10]
    ) or "<tr><td colspan='5'>—</td></tr>"
    running = any(i["status"] == "running" for i in _installs.values())
    body = f"""
    <h2>🛠 Админ-панель</h2>
    <p>Всего клиентов: <b>{s['customers']}</b> | Активных: <b>{s['active']}</b></p>
    <p>Оплаченных платежей: <b>{s['paid']}</b> | Выручка: <b>{fmt_price(s['revenue'])} ₽</b></p>
    <h3>Серверы:</h3>
    <table>
      <tr><th>ID</th><th>Имя</th><th>URL</th><th>Активных клиентов</th><th>Лимит</th></tr>
      {''.join(f"<tr><td>{srv['id']}</td><td>{html.escape(srv['name'])}</td><td>{html.escape(srv['base_url'])}</td><td>{counts.get(srv['id'], 0)}</td><td>{config.SERVER_MAX_USERS}</td></tr>" for srv in servers)}
    </table>
    <p class="note">Новые клиенты автоматически распределяются на наименее загруженный сервер (лимит {config.SERVER_MAX_USERS} пользователей на панель).</p>

    <h3 id="install">➕ Добавить сервер (автоустановка панели по SSH)</h3>
    <form method="post" action="/admin/server/install" class="card">
      <p><input name="name" placeholder="Имя сервера (например, de2)" required style="width:100%"></p>
      <p><input name="host" placeholder="IP-адрес сервера" required style="width:100%"></p>
      <p><input name="ssh_port" placeholder="Порт SSH" value="22" required style="width:100%"></p>
      <p><input name="ssh_user" placeholder="Логин SSH (обычно root)" required style="width:100%"></p>
      <p><input name="ssh_password" type="password" placeholder="Пароль SSH" required style="width:100%" autocomplete="off"></p>
      <p><button class="btn" type="submit">🚀 Установить панель на сервер</button></p>
      <p class="note">Панель WDTT установится автоматически (2–10 минут): скачается инсталлятор,
        поднимется панель, сменятся дефолтные логин/пароль, выставится лимит пользователей.
        Логин и пароль панели покажутся в списке установок ниже и в логах бота.</p>
    </form>

    <h3 id="installs">Установки серверов:</h3>
    <table>
      <tr><th>ID</th><th>Имя</th><th>Хост</th><th>Статус</th><th>Детали</th></tr>
      {installs_rows}
    </table>
    <p><a href='/'>На главную</a></p>
    """
    page = _page("Админ-панель", body, session)
    if running:
        # пока идёт установка — обновляем страницу, чтобы видеть статус
        page = page.replace("</head>", '<meta http-equiv="refresh" content="15"></head>', 1)
    return web.Response(text=page, content_type="text/html")


# ---------- админ: автоустановка сервера по SSH (фоном) ----------

_installs: dict[int, dict] = {}
_install_seq = 0


async def _run_install(install_id: int, name: str, host: str, ssh_port: int, ssh_user: str, ssh_password: str) -> None:
    """Фоновая установка панели WDTT на новый сервер через SSH."""
    from server_setup import SetupError, deploy_server
    from db import list_vk_accounts

    rec = _installs[install_id]
    try:
        accounts = await list_vk_accounts()
        vk_cookies = accounts[0]["cookies"] if accounts else None
        result = await deploy_server(
            host=host, ssh_port=ssh_port, ssh_user=ssh_user,
            ssh_password=ssh_password, name=name, vk_cookies=vk_cookies,
        )
        rec.update(status="done", detail=f"Сервер #{result['id']} добавлен: {result['base_url']} | логин панели: {result['panel_username']} | пароль: {result['panel_password']}")
        log.info("site-admin: сервер %s (%s) установлен как #%s", name, host, result["id"])
    except SetupError as e:
        rec.update(status="error", detail=str(e)[:1500])
        log.warning("site-admin: установка %s (%s) не удалась: %s", name, host, e)
    except Exception as e:
        rec.update(status="error", detail=str(e)[:1500])
        log.exception("site-admin: установка %s (%s)", name, host)
    finally:
        rec["ssh_password"] = None  # пароль SSH больше не храним


def _field(data, key: str) -> str:
    """Значение формы как строка (aiohttp может вернуть FileField для file-полей)."""
    v = data.get(key)
    return v if isinstance(v, str) else ""


async def handle_admin_install_server(request: web.Request) -> web.Response:
    """POST /admin/server/install — запускает автоустановку панели на новом сервере."""
    session = _require_site_session(request)
    tg_id = session.get("telegram_id")
    if not tg_id or not _is_admin(tg_id):
        return web.Response(text=_page("Ошибка", "<p class='err'>Доступ только для администратора.</p>"), status=403)
    data = await request.post()
    name = _field(data, "name").strip()
    host = _field(data, "host").strip()
    ssh_user = _field(data, "ssh_user").strip()
    ssh_password = _field(data, "ssh_password")
    try:
        ssh_port = int(_field(data, "ssh_port").strip() or "22")
    except ValueError:
        ssh_port = 0
    if not name or not host or not ssh_user or not ssh_password or not (1 <= ssh_port <= 65535):
        return web.Response(
            text=_page("Ошибка", "<p class='err'>Заполните все поля: имя, IP, порт SSH (1–65535), логин и пароль.</p>"),
            status=400,
        )
    global _install_seq
    _install_seq += 1
    iid = _install_seq
    _installs[iid] = {
        "id": iid, "name": name, "host": host, "status": "running",
        "detail": "", "started": int(time.time()), "ssh_password": None,
    }
    asyncio.create_task(_run_install(iid, name, host, ssh_port, ssh_user, ssh_password))
    log.info("site-admin tg=%s запустил автоустановку сервера «%s» (%s:%s, user=%s)", tg_id, name, host, ssh_port, ssh_user)
    raise web.HTTPFound("/admin#installs")

async def handle_settings_post(request: web.Request) -> web.Response:
    """POST /settings — сохранение галочек автопродления и уведомления (нужен привязанный Telegram)."""
    session = _require_site_session(request)
    tg_id = session.get("telegram_id")
    if not tg_id:
        raise web.HTTPFound("/bind-telegram")
    data = await request.post()
    # если магазин не поддерживает рекурренты — автопродление технически невозможно,
    # не даём включить его даже в обход UI
    from payments import recurring_supported
    if recurring_supported() is False:
        raise web.HTTPFound("/profile#settings")
    auto_renew = data.get("auto_renew") == "1"
    notify_on = data.get("notify_on") == "1"
    try:
        notify_hours = int(str(data.get("notify_hours") or "0") or 0) if notify_on else 0
    except (TypeError, ValueError):
        notify_hours = 0
    await set_auto_renew(tg_id, auto_renew, notify_hours)
    log.info("site: tg %s установил auto_renew=%s notify_hours=%s", tg_id, auto_renew, notify_hours)
    resp = web.HTTPFound("/profile#settings")
    return resp


async def handle_logout(request: web.Request) -> web.Response:
    response = web.HTTPFound("/")
    response.del_cookie("session_id")
    return response

# ---------- вспомогательные ----------

def fmt_price(price) -> str:
    try:
        f = float(price)
    except (TypeError, ValueError):
        return str(price)
    return f"{int(f)}" if f == int(f) else f"{f:.2f}"

def fmt_traffic(gb) -> str:
    if not gb:
        return "безлимит"
    rounded = float(gb)
    return f"{rounded:g} ГБ"

def fmt_bytes(n) -> str:
    n = float(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return f"{n:.1f} {unit}" if unit != "Б" else f"{int(n)} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"

def fmt_date(ts) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M")

async def has_used_trial_web(telegram_id: int) -> bool:
    async with db_conn() as db:
        cur = await db.execute("SELECT 1 FROM trials WHERE telegram_id = ?", (telegram_id,))
        return await cur.fetchone() is not None

async def _ensure_vk_hash(client: WdttClient, pwd: str) -> None:
    from config import WDTT_VK_HASH
    if WDTT_VK_HASH:
        return
    try:
        info = await client.get_user_info(pwd) or {}
        if WdttClient._clean_hash_pieces(str(info.get("vk_hash") or "")):
            return
        await client.create_vk_call(pwd)
    except Exception as e:
        log.warning("VK-вызов для %s не создан (%s)", pwd[:2], e)

# ---------- API для мобильного приложения ----------

async def api_plans(request: web.Request) -> web.Response:
    """GET /api/plans — список тарифов (JSON) для Android-приложения."""
    plans = []
    for p in await list_plans() or list(PLANS):
        plans.append({
            "id": p["id"],
            "name": p["name"],
            "price": float(p["price"]),
            "currency": "RUB",
            "days": p["days"],
            "traffic_gb": p.get("traffic_gb", 0),
            "max_devices": p.get("max_devices", 1),
        })
    return web.json_response({"plans": plans})


async def api_create_payment(request: web.Request) -> web.Response:
    """POST /api/create-payment — создать платёж ЮKassa.

    Тело JSON: {"telegram_id": int, "plan_id": str, "username": str?}
    Ответ: {"payment_id", "url", "amount", "currency"}
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)

    tg_id = data.get("telegram_id")
    plan_id = data.get("plan_id")
    if not tg_id or not plan_id:
        return web.json_response({"error": "missing fields"}, status=400)

    await init_db()
    plan = await get_plan(str(plan_id)) or (ALL_PLANS_BY_ID.get(str(plan_id)) if isinstance(ALL_PLANS_BY_ID, dict) else None)
    if not plan:
        return web.json_response({"error": "plan not found"}, status=404)

    try:
        payment = await create_payment(plan, int(tg_id))
    except Exception as e:
        log.exception("api create payment failed")
        return web.json_response({"error": str(e)}, status=500)

    if not payment.get("url"):
        return web.json_response({"error": "no payment url"}, status=500)

    await add_payment(
        telegram_id=int(tg_id),
        username=data.get("username", ""),
        payment_id=payment["id"],
        plan_id=str(plan["id"]),
        amount=f"{float(plan['price']):.2f}",
    )

    return web.json_response({
        "payment_id": payment["id"],
        "url": payment["url"],
        "amount": float(plan["price"]),
        "currency": "RUB",
    })


async def api_check_payment(request: web.Request) -> web.Response:
    """GET /api/check-payment?payment_id=... — статус платежа.

    Ответ: {"status": "pending" | "succeeded", "payment_id": "..."}
    """
    payment_id = request.query.get("payment_id")
    if not payment_id:
        return web.json_response({"error": "missing payment_id"}, status=400)

    await init_db()
    row = await get_payment(payment_id)
    if not row:
        return web.json_response({"error": "not found"}, status=404)

    try:
        yk = await fetch_payment(payment_id)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=502)

    status = yk.get("status", "pending") if isinstance(yk, dict) else "pending"
    granted = (row.get("status") or "") == "fulfilled"
    if status == "succeeded" and not granted:
        # Выдаём доступ через общий fulfillment (он сам проверит платёж и
        # запишет fulfilled). Раньше здесь статус ставился вручную, из-за чего
        # платёж «сгорал» без выдачи подписки.
        try:
            from bot import fulfill_payment
            await fulfill_payment(payment_id)
            fresh = await get_payment(payment_id) or row
            granted = (fresh.get("status") or "") == "fulfilled"
        except Exception:
            log.exception("api_check_payment: fulfillment %s", payment_id)

    return web.json_response({
        "status": status,
        "payment_id": payment_id,
        "granted": granted,
    })

# ---------- реферальная ссылка сайта ----------

async def handle_referral_link(request: web.Request) -> web.Response:
    """Переход по реферальной ссылке сайта /r/<code> — запоминаем реф-код в куке."""
    code = request.match_info.get("code")
    await init_db()
    site_user = await get_site_user_by_referral_code(code)
    if not site_user:
        return web.Response(text=_page("Ошибка", "<p class='err'>Реферальная ссылка не найдена.</p><a href='/register'>Регистрация</a>"), content_type="text/html")
    response = web.HTTPFound("/register")
    response.set_cookie("ref_code", code, max_age=86400 * 30, httponly=True, samesite="Lax", path="/")
    return response


# ---------- 404 ----------

async def handle_404(request: web.Request) -> web.Response:
    body = """
    <div class="card" style="text-align:center">
      <h3>404 — страница не найдена</h3>
      <p>Вернитесь на <a href="/">главную</a>.</p>
    </div>
    """
    return web.Response(text=_page("404", body, _get_session(request)), status=404, content_type="text/html")


# ---------- приложение ----------

def make_app() -> web.Application:
    app = web.Application()
    # статика сайта (CSS/JS)
    app.router.add_static("/static/", path=os.path.join(os.path.dirname(__file__), "static"), name="static")
    app.router.add_get("/", handle_index)
    app.router.add_get("/register", handle_register_get)
    app.router.add_post("/register", handle_register_post)
    app.router.add_get("/login", handle_login_get)
    app.router.add_post("/login", handle_login_post)
    app.router.add_get("/login/telegram", handle_login_telegram)
    app.router.add_get("/login/auto", handle_login_auto)
    app.router.add_get("/auth/telegram/confirm", handle_login_confirm)
    app.router.add_get("/auth/telegram", handle_auth_telegram)
    app.router.add_get("/bind-telegram", handle_bind_telegram)
    app.router.add_get("/r/{code}", handle_referral_link)
    app.router.add_get("/profile", handle_profile)
    app.router.add_post("/settings", handle_settings_post)
    app.router.add_get("/buy/{plan_id}", handle_buy)
    app.router.add_get("/pay/success", handle_pay_success)
    app.router.add_get("/pay/cancel", handle_pay_cancel)
    app.router.add_post("/yookassa-webhook", handle_yookassa_webhook)
    app.router.add_get("/instruction", handle_instruction)
    app.router.add_get("/admin", handle_admin)
    app.router.add_post("/admin/server/install", handle_admin_install_server)
    app.router.add_get("/logout", handle_logout)
    app.router.add_get("/{tail:.*}", handle_404)

    # API для Android-приложения (покупка подписки из приложения)
    app.router.add_get("/api/plans", api_plans)
    app.router.add_post("/api/create-payment", api_create_payment)
    app.router.add_get("/api/check-payment", api_check_payment)

    # Единый API экосистемы (тот же, что у мини-аппа и будущих приложений)
    from api import register_v1_routes
    register_v1_routes(app)
    return app

async def init_on_start(app: web.Application) -> None:
    await init_db()
    # фоновая очистка протухших сессий сайта (иначе SESSIONS растёт бесконечно)
    async def _sessions_janitor():
        while True:
            await asyncio.sleep(600)
            try:
                _prune_sessions()
            except Exception:
                log.exception("janitor: очистка сессий")
    app["_sessions_janitor_task"] = asyncio.create_task(_sessions_janitor())

async def run_site() -> None:
    """Запуск сайта K.O. Gateway (порт WEB_PORT) как фоновой задачи бота."""
    app = make_app()
    app.on_startup.append(init_on_start)
    runner = web.AppRunner(app)
    await runner.setup()
    host = os.getenv("WEB_HOST", "0.0.0.0")
    port = int(os.getenv("WEB_PORT", "8080"))
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("Сайт K.O. Gateway слушает на http://%s:%s/ (публичный URL: %s)",
             host, port, SITE_URL or "не задан SITE_URL")
    await asyncio.Event().wait()  # держим сервер живым


if __name__ == "__main__":
    app = make_app()
    app.on_startup.append(init_on_start)
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("WEB_PORT", "8080")))