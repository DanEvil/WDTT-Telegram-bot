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
import time
import uuid
from collections import deque
from datetime import datetime, timezone

from aiohttp import web

from config import (
    ADMIN_IDS, TELEGRAM_TOKEN, RETURN_URL, DB_PATH,
    YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY,
)
from db import (
    conn as db_conn, init_db, get_customer, upsert_customer,
    add_payment, get_payment, set_payment_status, get_plan, list_plans,
    claim_trial, list_servers, stats,
)
from payments import create_payment, fetch_payment
from plans import PLANS, TRIAL, ALL_PLANS_BY_ID
from wdtt import WdttClient, WdttError

log = logging.getLogger("web")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# ---------- простые сессии в памяти ----------
SESSION_SECRET = os.getenv("WEB_SESSION_SECRET", os.urandom(24).hex())
SESSIONS: dict[str, dict] = {}  # session_id -> {"telegram_id": int, "username": str|None, "created": float}

def _get_session(request: web.Request) -> dict | None:
    sid = request.cookies.get("session_id")
    if sid and sid in SESSIONS:
        return SESSIONS[sid]
    return None

def set_session(response: web.Response, telegram_id: int, username: str) -> None:
    sid = uuid.uuid4().hex
    SESSIONS[sid] = {"telegram_id": telegram_id, "username": username, "created": time.time()}
    response.set_cookie("session_id", sid, httponly=True, samesite="Lax", path="/")

def _require_session(request: web.Request) -> dict | None:
    s = _get_session(request)
    if not s:
        raise web.HTTPFound("/")
    return s

def _is_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_IDS

# ---------- вспомогательные HTML ----------

def _page(title: str, body: str, session: dict | None = None) -> str:
    auth = ""
    if session:
        auth = (
            f"<div style='text-align:right;margin:8px;'>"
            f"Вход как <b>{html.escape(session.get('username') or session['telegram_id'])}</b> "
            f"| <a href='/logout'>Выход</a>"
            f"</div>"
        )
    else:
        auth = (
            "<div style='text-align:right;margin:8px;'>"
            "<a href='/'>Войти через Telegram</a>"
            "</div>"
        )
    nav = ""
    if session and _is_admin(session["telegram_id"]):
        nav = "<a href='/admin'>Админ-панель</a> | "
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — WDTT</title>
<style>
  body{{font-family:sans-serif;max-width:700px;margin:2rem auto;padding:0 1rem;line-height:1.5;color:#222}}
  a{{color:#0066cc;text-decoration:none}} a:hover{{text-decoration:underline}}
  .btn{{display:inline-block;padding:.5rem 1rem;background:#0066cc;color:#fff;border-radius:.4rem;text-decoration:none}}
  .btn.secondary{{background:#6c757d}}
  .card{{border:1px solid #ddd;border-radius:.6rem;padding:1rem;margin:1rem 0}}
  .plan{{border:1px solid #ddd;border-radius:.6rem;padding:1rem;margin:.5rem 0}}
  .plan h3{{margin:.2rem 0}} .price{{font-size:1.2rem;font-weight:bold;color:#0066cc}}
  .nav{{margin-bottom:1rem}}
  .nav a{{margin-right:.8rem}}
  .mono{{font-family:monospace;background:#f4f4f4;padding:.2rem .4rem;border-radius:.3rem;word-break:break-all}}
  .ok{{color:#0a7a0a}} .err{{color:#b00020}}
  table{{border-collapse:collapse;width:100%}}
  th,td{{border:1px solid #ddd;padding:.4rem;text-align:left}}
  th{{background:#f4f4f4}}
</style>
</head>
<body>
{auth}
<nav class="nav"><a href="/">Главная</a>{nav}<a href="/instruction">Инструкция</a></nav>
<h1>{html.escape(title)}</h1>
{body}
<hr>
<p><small>WDTT VPN — продажа подписок. Поддержка: через бота в Telegram.</small></p>
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
    plans_html = ""
    for p in list_plans() or PLANS:
        plans_html += f"""
        <div class="plan">
          <h3>{html.escape(p['name'])}</h3>
          <p>{html.escape(p.get('description',''))}</p>
          <div class="price">{fmt_price(p['price'])} ₽</div>
          <p>Срок: {p['days']} дн. · Трафик: {'безлимит' if not p.get('traffic_gb') else p['traffic_gb']} ГБ · Устройств: {p.get('max_devices',1)}</p>
          <a href="/buy/{p['id']}" class="btn">Купить</a>
        </div>
        """
    trial_html = ""
    if session:
        used = await has_used_trial_web(session["telegram_id"])
        if not used:
            trial_html = f"""
            <div class="card">
              <h3>🎁 Пробный период</h3>
              <p>{TRIAL['days']} дн. · Трафик: {fmt_traffic(TRIAL['traffic_gb'])} · Устройств: {TRIAL['max_devices']}</p>
              <a href="/buy/trial" class="btn secondary">Активировать пробный период</a>
            </div>
            """
    body = f"""
    <h2>Купить подписку WDTT</h2>
    {trial_html}
    <h3>Тарифы:</h3>
    {plans_html or "<p>Тарифы не настроены.</p>"}
    <p>После оплаты вы получите ссылку <code>wdtt://</code> для подключения в приложении WDTT.</p>
    """
    return web.Response(text=_page("Главная", body, session), content_type="text/html")

async def handle_auth_telegram(request: web.Request) -> web.Response:
    """Приём данных от Telegram Login Widget."""
    data = dict(request.query)
    if not _csrf_check_telegram(data):
        return web.Response(text=_page("Ошибка", "<p class='err'>Не удалось подтвердить данные от Telegram.</p>"), status=403)

    telegram_id = int(data["id"])
    username = data.get("username")
    first_name = data.get("first_name", "")

    await init_db()
    # создаём/обновляем пользователя в базе (без пароля WDTT — он появится после оплаты)
    async with db_conn() as db:
        await db.execute(
            "INSERT INTO customers (telegram_id, username, expires_at, plan_id, server_id, created_at) "
            "VALUES (?, ?, 0, NULL, NULL, ?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET username=COALESCE(excluded.username, customers.username)",
            (telegram_id, username or first_name or "", int(time.time())),
        )

    response = web.HTTPFound("/profile")
    set_session(response, telegram_id, username or first_name or str(telegram_id))
    return response

async def handle_profile(request: web.Request) -> web.Request:
    session = _require_session(request)
    tg_id = session["telegram_id"]
    await init_db()
    customer = await get_customer(tg_id)
    now = int(time.time())

    body = ""
    if not customer or not customer["wdtt_password"]:
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

        # ссылка wdtt://
        link = "—"
        try:
            servers = await list_servers()
            sid = customer["server_id"] or (servers[0]["id"] if servers else None)
            if sid:
                server = await get_server(sid)
                client = WdttClient(server["base_url"], server["username"], server["password"], verify_ssl=bool(server["verify_ssl"]))
                await client.login()
                link = await client.find_link(customer["wdtt_password"])
                await client.close()
        except Exception as e:
            log.warning("не удалось получить ссылку wdtt://: %s", e)
            link = "ссылка временно недоступна"

        body += f"""
        <div class="card">
          <h3>📊 Ваша подписка</h3>
          <p>Тариф: <b>{html.escape(str(plan_name))}</b></p>
          <p>Действует до: <b>{fmt_date(expires_at)}</b> (осталось {days} дн. {hours} ч)</p>
          <p>Статус: <span class="{'ok' if active else 'err'}">{'Активна' if active else 'Истекла'}</span></p>
          <h4>🔗 Ссылка для подключения:</h4>
          <div class="mono">{html.escape(str(link))}</div>
          <p>Скопируйте эту ссылку в приложение WDTT (Android / iOS / Windows).</p>
          <a href="/buy/{customer['plan_id']}" class="btn secondary">Продлить подписку</a>
        </div>
        """

    body += "<a href='/'>На главную</a>"
    return web.Response(text=_page("Профиль", body, session), content_type="text/html")

async def handle_buy(request: web.Request) -> web.Response:
    session = _require_session(request)
    plan_id = request.match_info.get("plan_id")
    await init_db()
    plan = await get_plan(plan_id) or ALL_PLANS_BY_ID.get(plan_id)
    if not plan:
        return web.Response(text=_page("Ошибка", "<p class='err'>Тариф не найден.</p>"), status=404)

    if plan_id == "trial":
        if not await claim_trial(session["telegram_id"]):
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
                comment=f"web:{session['telegram_id']}",
                expires_at=expires,
                total_gb=int(plan.get("traffic_gb") or 0),
                max_devices=int(plan.get("max_devices") or 1),
                vk_hash="",
            )
            await _ensure_vk_hash(client, pwd)
            await upsert_customer(session["telegram_id"], session.get("username"), expires, plan["id"], sid,
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

    # платный тариф — создаём платёж ЮKassa
    try:
        payment = await create_payment(plan, session["telegram_id"])
    except Exception as e:
        log.exception("ошибка создания платежа")
        return web.Response(text=_page("Ошибка", f"<p class='err'>Не удалось создать платёж: {html.escape(str(e))}</p>"))

    if not payment.get("url"):
        return web.Response(text=_page("Ошибка", "<p class='err'>ЮKassa не вернула ссылку на оплату.</p>"))

    await add_payment(
        telegram_id=session["telegram_id"],
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

async def handle_yookassa_webhook(request: web.Request) -> web.Response:
    """Вебхук ЮKassa — триггер для fulfillment (проверяем через API)."""
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="bad json")
    if data.get("type") != "notification" or data.get("event") != "payment.succeeded":
        return web.Response(text="ok")
    obj = data.get("object") or {}
    payment_id = obj.get("id")
    if not payment_id:
        return web.Response(text="ok")
    # перепроверяем через API и выполняем fulfillment
    try:
        from bot import fulfill_payment
        asyncio.create_task(fulfill_payment(str(payment_id)))
    except Exception as e:
        log.exception("webhook: не удалось запустить fulfilment: %s", e)
    return web.Response(text="ok")

async def handle_instruction(request: web.Request) -> web.Response:
    body = """
    <h2>📖 Инструкция подключения</h2>
    <p><b>Android:</b> установите приложение WDTT, откройте вкладку «Туннель», включите «Режим ссылки»,
    вставьте вашу <code>wdtt://</code> ссылку и нажмите «Подключить».</p>
    <p><b>iOS:</b> установите TestFlight из App Store, перейдите по пригласительной ссылке клиента WDTT,
    установите приложение и добавьте конфигурацию по ссылке.</p>
    <p><b>Windows:</b> используйте qWDTT или другой клиент, поддерживающий <code>wdtt://</code>.</p>
    <a href='/'>На главную</a>
    """
    return web.Response(text=_page("Инструкция", body, _get_session(request)), content_type="text/html")

# ---------- админ ----------

async def handle_admin(request: web.Request) -> web.Response:
    session = _require_session(request)
    if not _is_admin(session["telegram_id"]):
        return web.Response(text=_page("Ошибка", "<p class='err'>Доступ только для администратора.</p>"), status=403)
    await init_db()
    s = await stats()
    servers = await list_servers()
    body = f"""
    <h2>🛠 Админ-панель</h2>
    <p>Всего клиентов: <b>{s['customers']}</b> | Активных: <b>{s['active']}</b></p>
    <p>Оплаченных платежей: <b>{s['paid']}</b> | Выручка: <b>{fmt_price(s['revenue'])} ₽</b></p>
    <h3>Серверы WDTT:</h3>
    <table>
      <tr><th>ID</th><th>Имя</th><th>URL</th></tr>
      {''.join(f"<tr><td>{srv['id']}</td><td>{html.escape(srv['name'])}</td><td>{html.escape(srv['base_url'])}</td></tr>" for srv in servers)}
    </table>
    <p><a href='/'>На главную</a></p>
    """
    return web.Response(text=_page("Админ-панель", body, session), content_type="text/html")

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
    for p in list_plans() or list(PLANS):
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

# ---------- приложение ----------

def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/auth/telegram", handle_auth_telegram)
    app.router.add_get("/profile", handle_profile)
    app.router.add_get("/buy/{plan_id}", handle_buy)
    app.router.add_get("/pay/success", handle_pay_success)
    app.router.add_get("/pay/cancel", handle_pay_cancel)
    app.router.add_post("/yookassa-webhook", handle_yookassa_webhook)
    app.router.add_get("/instruction", handle_instruction)
    app.router.add_get("/admin", handle_admin)
    app.router.add_get("/logout", handle_logout)

    # API для Android-приложения (покупка подписки из приложения)
    app.router.add_get("/api/plans", api_plans)
    app.router.add_post("/api/create-payment", api_create_payment)
    app.router.add_get("/api/check-payment", api_check_payment)
    return app

async def init_on_start(app: web.Application) -> None:
    await init_db()

if __name__ == "__main__":
    app = make_app()
    app.on_startup.append(init_on_start)
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("WEB_PORT", "8080")))
