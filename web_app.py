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
from db import (
    conn as db_conn, init_db, get_customer, upsert_customer,
    add_payment, get_payment, list_payments, set_payment_status, get_plan, list_plans,
    claim_trial, list_servers, stats, get_server,
    get_referrals_count, get_site_user_by_email,
    generate_referral_code, add_site_referral, get_site_referrals_count,
    get_site_user_by_referral_code,
    get_auto_renew_settings, set_auto_renew,
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
    "%3Ctext y='.9em' font-size='90'%3E%F0%9F%94%92%3C/text%3E%3C/svg%3E"
)

def _page(title: str, body: str, session: dict | None = None, active: str = "") -> str:
    if session:
        who = session.get("username") or session.get("email") or "Профиль"
        auth = (
            f"<div class='user'>Вход как <b>{html.escape(str(who))}</b> "
            f"| <a href='/profile'>Профиль</a> | <a href='/logout'>Выход</a></div>"
        )
    else:
        auth = "<div class='user'><a href='/login'>Войти</a> | <a href='/register'>Регистрация</a></div>"

    nav = ""
    if session and session.get("telegram_id") and _is_admin(session["telegram_id"]):
        nav += "<a href='/admin'>Админ-панель</a>"

    def navlink(href: str, label: str) -> str:
        cls = " class='active'" if active == label else ""
        return f"<a href='{href}'{cls}>{label}</a>"

    nav_html = (
        navlink("/", "Главная")
        + navlink("/instruction", "Инструкция")
        + nav
    )
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
<meta name="theme-color" content="#2563eb">
<meta name="description" content="{html.escape(BRAND)} — управление подпиской, подключение и оплата">
<meta property="og:title" content="{html.escape(title)} — {html.escape(BRAND)}">
<meta property="og:description" content="Подписка и подключение без VPN: Android, iOS, Windows">
<meta property="og:type" content="website">
<link rel="icon" href="{_FAVICON}">
<link rel="stylesheet" href="/static/site.css">
<title>{html.escape(title)} — {html.escape(BRAND)}</title>
</head>
<body>
<a class="skip-link" href="#main">Перейти к содержимому</a>
<header class="topbar">
  <div class="brand">{_brand_html()}</div>
  <div class="topbar-right">{tg_btn}{auth}</div>
</header>
<nav class="nav">{nav_html}</nav>
<main id="main">
<h1 class="page-title">{html.escape(title)}</h1>
{body}
</main>
<footer class="footer">{html.escape(BRAND)} — стабильный доступ без VPN. Управление подпиской и поддержка: <a href="{_bot_link() or '#'}">бот в Telegram</a>{' · ' + f'<a href="{_bot_link()}">t.me/{html.escape(BOT_USERNAME)}</a>' if BOT_USERNAME else ''}.</footer>
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
    if session:
        if not await has_used_trial_web(_sub_id(session)):
            trial_html = f"""
                <div class="card trial-card">
                  <h3>🎁 Пробный период</h3>
                  <p>{TRIAL['days']} дн. · Трафик: {fmt_traffic(TRIAL['traffic_gb'])} · Устройств: {TRIAL['max_devices']}</p>
                  <a href="/buy/trial" class="btn secondary">Активировать пробный период</a>
                </div>
                """
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
    {trial_html}
    <div id="plans"></div>
    <h2>Тарифы</h2>
    {plans_html or "<p>Тарифы не настроены.</p>"}
    {apps_html}
    <p style="color:var(--muted);font-size:.9rem">После оплаты вы получите ссылку <code>wdtt://</code> для подключения в приложении.</p>
    """
    return web.Response(text=_page("Главная", body, session, active="Главная"), content_type="text/html")

# ---------- регистрация / вход на сайт ----------

_REGISTER_FORM = """
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
  <p style="text-align:center;margin-top:1rem;color:var(--muted);font-size:.9rem">
    Уже есть аккаунт? <a href="/login">Войти</a>
  </p>
</div>
"""

_LOGIN_FORM = """
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
    return web.Response(text=_page("Регистрация", _REGISTER_FORM, session), content_type="text/html")

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
    return web.Response(text=_page("Вход", _LOGIN_FORM, session), content_type="text/html")

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

        # ссылка wdtt:// и трафик из панели
        link = "—"
        used_bytes = 0
        total_gb = int(plan.get("traffic_gb") or 0) if plan else 0
        try:
            servers = await list_servers()
            sid = customer["server_id"] or (servers[0]["id"] if servers else None)
            if sid:
                server = await get_server(sid)
                client = WdttClient(server["base_url"], server["username"], server["password"], verify_ssl=bool(server["verify_ssl"]))
                await client.login()
                link = await client.find_link(customer["wdtt_password"])
                info = await client.get_user_info(customer["wdtt_password"]) or {}
                used_bytes = int(info.get("up_bytes") or 0) + int(info.get("down_bytes") or 0)
                await client.close()
        except Exception as e:
            log.warning("не удалось получить ссылку wdtt://: %s", e)
            link = "ссылка временно недоступна"

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
          <div class="mono"><code>{html.escape(str(link))}</code></div>
          <p style="color:var(--muted);font-size:.85rem">Скопируйте ссылку в приложение WDTT (Android / iOS / Windows).</p>
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
        body += f"""
    <div class="card" id="settings">
      <h3>🔄 Автопродление подписки</h3>
      <form method="post" action="/settings">
        <label class="check">
          <input type="checkbox" name="auto_renew" value="1"{' checked' if ar['auto_renew'] else ''}>
          <span>Автоматически продлевать подписку на <b>1 месяц (30 дней)</b> —
            при окончании спишем <b>{month_price} ₽</b> с сохранённой карты ЮKassa.</span>
        </label>
        {"<p style='color:var(--err);font-size:.85rem'>⚠️ Сначала оплатите любой тариф картой через ЮKassa — карта сохранится для автопродления.</p>" if (ar['auto_renew'] and not ar['has_payment_method']) else ''}
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
    if tg_id:
        payments = await list_payments(tg_id, limit=10)
        if payments:
            rows_html = ""
            for p in payments:
                st = p.get("status") or ""
                st_badge = {
                    "fulfilled": "ok", "paid": "ok", "succeeded": "ok",
                    "pending": "", "cancelled": "err",
                }.get(st, "")
                st_text = {"fulfilled": "Оплачен", "paid": "Оплачен", "succeeded": "Оплачен",
                           "pending": "Ожидает", "cancelled": "Отменён"}.get(st, st)
                rows_html += (
                    f"<tr><td>{fmt_date(p['created_at'])}</td>"
                    f"<td>{html.escape(str(p.get('plan_id') or ''))}</td>"
                    f"<td>{fmt_price(p.get('amount'))} ₽</td>"
                    f"<td><span class='badge {st_badge}'>{st_text}</span></td></tr>"
                )
            body += f"""
    <div class="card">
      <h3>💳 История платежей</h3>
      <table>
        <tr><th>Дата</th><th>Тариф</th><th>Сумма</th><th>Статус</th></tr>
        {rows_html}
      </table>
    </div>
    """

    body += "<a href='/'>На главную</a> | <a href='/logout'>Выход</a>"
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
        # пробный период — выдаём сразу без оплаты
        try:
            servers = await list_servers()
            sid = None
            if servers:
                sid = servers[0]["id"]
            if not sid:
                return web.Response(text=_page("Ошибка", "<p class='err'>Нет настроенных серверов WDTT.</p>"))
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
    if payment_id:
        await init_db()
        row = await get_payment(payment_id)
        if row:
            try:
                yk = await fetch_payment(payment_id)
                if yk and yk["status"] == "succeeded":
                    await set_payment_status(payment_id, "fulfilled")
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
      <p>Скопируйте вашу ссылку <code>wdtt://...</code> из профиля на сайте или из бота Telegram.</p>
      <p><b>Android:</b> откройте приложение, вкладку «Туннель», включите «Режим ссылки»,
         вставьте <code>wdtt://</code> ссылку и нажмите «Подключить».</p>
      <p><b>iOS:</b> установите TestFlight из App Store, перейдите по
         <a href="{APP_IOS_JOIN_URL}" target="_blank" rel="noopener">пригласительной ссылке</a> клиента,
         установите приложение и импортируйте конфигурацию: в приложении откройте «Settings» →
         «Backup &amp; Restore» → «Import from Connection Link…» и разрешите вставку — ссылка
         подставится из буфера обмена (см. скриншоты в боте, команда «📖 Инструкция»).</p>
      <p><b>Windows:</b> используйте qWDTT или другой клиент, поддерживающий <code>wdtt://</code>.</p>
    </div>
    <div class="card">
      <h3>3. Подключитесь</h3>
      <p>Нажмите «Подключить». Готово — весь трафик идёт через защищённый туннель.</p>
    </div>
    <p><a href='/'>На главную</a>{' · ' + f'<a href="{_bot_link()}">🤖 Открыть бота</a>' if BOT_USERNAME else ''}</p>
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
    body = f"""
    <h2>🛠 Админ-панель</h2>
    <p>Всего клиентов: <b>{s['customers']}</b> | Активных: <b>{s['active']}</b></p>
    <p>Оплаченных платежей: <b>{s['paid']}</b> | Выручка: <b>{fmt_price(s['revenue'])} ₽</b></p>
    <h3>Серверы:</h3>
    <table>
      <tr><th>ID</th><th>Имя</th><th>URL</th></tr>
      {''.join(f"<tr><td>{srv['id']}</td><td>{html.escape(srv['name'])}</td><td>{html.escape(srv['base_url'])}</td></tr>" for srv in servers)}
    </table>
    <p><a href='/'>На главную</a></p>
    """
    return web.Response(text=_page("Админ-панель", body, session), content_type="text/html")

async def handle_settings_post(request: web.Request) -> web.Response:
    """POST /settings — сохранение галочек автопродления и уведомления (нужен привязанный Telegram)."""
    session = _require_site_session(request)
    tg_id = session.get("telegram_id")
    if not tg_id:
        raise web.HTTPFound("/bind-telegram")
    data = await request.post()
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
    if status == "succeeded":
        await set_payment_status(payment_id, "fulfilled")

    return web.json_response({"status": status, "payment_id": payment_id})

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
