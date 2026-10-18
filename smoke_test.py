"""Smoke test: WdttClient против заглушки панели WDTT + слой SQLite + payments.

Запуск: venv python smoke_test.py (из папки проекта).
"""
import asyncio
import os
import sys
import tempfile
import time

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "bot.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aiohttp import web

PANEL_USER = "admin"
PANEL_PASS = "secret"
state = {"sessions_valid": True, "next_num": 1}
users = {}  # password_key -> entry


def ok(obj=None):
    return web.json_response({"success": True, "obj": obj})


def err(msg, code=200):
    return web.json_response({"success": False, "msg": msg}, status=code)


async def login(request):
    if request.method != "POST":
        return web.Response(status=405, text="method not allowed")
    form = await request.post()
    if form.get("username") != PANEL_USER or form.get("password") != PANEL_PASS:
        return err("wrong username or password")
    state["sessions_valid"] = True  # новый логин -> новая валидная сессия
    resp = ok(None)
    resp.set_cookie("wdtt-panel", "sess-1")
    resp.set_cookie("wdtt-csrf", "csrf-1")
    return resp


def auth_guard(request):
    if request.cookies.get("wdtt-panel") != "sess-1" or not state["sessions_valid"]:
        return err("login again", 401)
    return None


def csrf_guard(request):
    if request.headers.get("X-CSRF-Token") != request.cookies.get("wdtt-csrf"):
        return err("invalid csrf token", 403)
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return web.Response(status=302, headers={"Location": "/wdtt/"})
    return None


async def users_add(request):
    g = auth_guard(request) or csrf_guard(request)
    if g:
        return g
    body = await request.json()
    pw = "userpass-%d" % state["next_num"]
    state["next_num"] += 1
    users[pw] = {
        "password": pw[:2] + "***",
        "password_key": pw,
        "comment": body.get("comment", ""),
        "expires_at": body.get("expires_at", 0),
        "total_gb": body.get("total_gb", 0),
        "max_devices": body.get("max_devices", 1),
        "active": True,
        "vk_hash": body.get("vk_hash", ""),
        "link": "wdtt://QWJjRGVm%s==" % state["next_num"],
        "sub_url": "%s://%s/subpage?pw=%s" % (request.scheme, request.host, pw),
    }
    return ok({"password": pw})


async def sub_page(request):
    # HTML-страница подписки: платформенные ссылки в скрытых textarea,
    # как на реальной странице панели (buildAllSubscriptionLinks).
    pw = request.query.get("pw")
    if pw not in users:
        return web.Response(status=404, text="not found")
    pw = pw
    # хэш есть в записи панели, но панель отдала colon-ссылки с плейсхолдером VK_HASH
    body = (
        '<textarea id="subscription-links" style="display:none">'
        "wdtt://universal-json\n"
        "wdtt://94.156.114.115:56000:56001::userpass-x:VK_HASH#VK Turn\n"
        "wdtt://94.156.114.115:56000:56001:9000:userpass-x:VK_HASH\n"
        "wdtt://94.156.114.115:56000:56001::userpass-x:VK_HASH#WDTT\n"
        "</textarea>\n"
        '<textarea id="subscription-link-titles" style="display:none">'
        "WDTT JSON\n"
        "iOS — VK Turn Proxy\n"
        "Android — WDTT\n"
        "PWDTT — Desktop\n"
        "</textarea>"
    )
    return web.Response(text=body, content_type="text/html")


async def users_list(request):
    g = auth_guard(request)
    if g:
        return g
    return ok({"users": list(users.values()), "inbound": {}})


async def users_update(request):
    g = auth_guard(request) or csrf_guard(request)
    if g:
        return g
    body = await request.json()
    old = body.get("old_password")
    if old not in users:
        return err("user not found")
    e = users[old]
    new = body.get("password") or old
    if new != old:
        users[new] = {**e, "password_key": new, "password": new[:2] + "***"}
        del users[old]
        e = users[new]
    e["expires_at"] = body.get("expires_at", 0)
    e["total_gb"] = body.get("total_gb", 0)
    e["max_devices"] = body.get("max_devices", 1)
    e["vk_hash"] = body.get("vk_hash", "")
    return ok(None)


async def vk_call_create(request):
    g = auth_guard(request) or csrf_guard(request)
    if g:
        return g
    body = await request.json()
    st = users.get(body.get("password"))
    if st is None:
        return err("user not found")
    merged = st.get("vk_hash", "")
    merged = (merged + "," if merged else "") + "vkhash-" + body["password"]
    users[body["password"]] = {**st, "vk_hash": merged}
    return ok({"join_link": "https://vk.ru/call/join/x", "vk_hash": merged})


async def users_delete(request):
    g = auth_guard(request) or csrf_guard(request)
    if g:
        return g
    body = await request.json()
    users.pop(body.get("password"), None)
    return ok(None)


async def users_reset_traffic(request):
    g = auth_guard(request) or csrf_guard(request)
    if g:
        return g
    return ok(None)


async def inbound(request):
    g = auth_guard(request)
    if g:
        return g
    return ok({"max_users": 10, "active_users": len(users), "online_users": 0})


async def make_app():
    app = web.Application()
    app.router.add_post("/wdtt/login", login)
    app.router.add_post("/wdtt/panel/api/users/add", users_add)
    app.router.add_get("/wdtt/panel/api/users", users_list)
    app.router.add_post("/wdtt/panel/api/users/update", users_update)
    app.router.add_post("/wdtt/panel/api/users/delete", users_delete)
    app.router.add_post("/wdtt/panel/api/users/reset-traffic", users_reset_traffic)
    app.router.add_post("/wdtt/panel/api/vk/call/create", vk_call_create)
    app.router.add_get("/wdtt/panel/api/inbound", inbound)
    app.router.add_get("/subpage", sub_page)
    return app


async def test_wdtt_client(base_url):
    from wdtt import WdttClient, WdttError

    now = int(time.time())

    # успешный логин + add_user
    c = WdttClient(base_url, "admin", "secret", verify_ssl=False)
    pw = await c.add_user(comment="tg:1", expires_at=now + 3600, total_gb=50, max_devices=2)
    assert pw.startswith("userpass-"), pw

    info = await c.get_user_info(pw)
    assert info["expires_at"] == now + 3600
    assert info["total_gb"] == 50
    assert info["max_devices"] == 2

    link = await c.find_link(pw)
    assert link == "wdtt://QWJjRGVm%d==" % state["next_num"]
    assert link and link.startswith("wdtt://"), link

    # неверный пароль -> WdttError
    c2 = WdttClient(base_url, "admin", "nope", verify_ssl=False)
    try:
        await c2.add_user(comment="x")
    except WdttError:
        pass
    else:
        raise AssertionError("expected login failure")

    # истёкшая сессия -> авто-перелогин и повтор запроса
    state["sessions_valid"] = False
    link2 = await c.find_link(pw)
    assert link2 == link
    state["sessions_valid"] = True

    # платформенные ссылки (только iOS/Android) + вписанный vk_hash панели
    users[pw]["vk_hash"] = "vkhash1,vkhash2"
    pl = await c.platform_links(pw)
    assert set(pl) == {"ios", "android"}, pl
    assert pl["ios"] == "wdtt://94.156.114.115:56000:56001::userpass-x:vkhash1#VK Turn", pl["ios"]
    assert pl["android"] == "wdtt://94.156.114.115:56000:56001:9000:userpass-x:vkhash1,vkhash2", pl["android"]
    assert all(x != "wdtt://universal-json" for x in pl.values())

    # универсальная JSON-ссылка: hash отсутствует/плейсхолдер -> вписываем
    import base64 as _b64u
    import json as _jsonu
    payload = {"ps": "WDTT", "add": "1.2.3.4", "dtls": 56000, "pass": pw}
    uni = "wdtt://" + _b64u.b64encode(_jsonu.dumps(payload).encode()).decode("ascii")
    patched = c._ensure_payload_hash(uni, "vk9")
    obj = _jsonu.loads(_b64u.b64decode(patched.split("wdtt://", 1)[1]))
    assert obj["hash"] == "vk9" and obj["pass"] == pw and obj["add"] == "1.2.3.4"
    payload2 = {"ps": "WDTT", "add": "1.2.3.4", "pass": pw, "hash": "VK_HASH"}
    uni2 = "wdtt://" + _b64u.b64encode(_jsonu.dumps(payload2).encode()).decode("ascii")
    obj2 = _jsonu.loads(_b64u.b64decode(c._ensure_payload_hash(uni2, "vk9").split("wdtt://", 1)[1]))
    assert obj2["hash"] == "vk9"
    # реальный хэш в payload не трогается
    payload3 = {"ps": "WDTT", "add": "1.2.3.4", "pass": pw, "hash": "real"}
    uni3 = "wdtt://" + _b64u.b64encode(_jsonu.dumps(payload3).encode()).decode("ascii")
    assert c._ensure_payload_hash(uni3, "vk9") == uni3

    # VK-вызов: hash создаётся и применяется к пользователю
    h = await c.create_vk_call(pw)
    assert "vkhash-" + pw in h, h
    info = await c.get_user_info(pw)
    assert "vkhash-" + pw in info["vk_hash"], info["vk_hash"]

    # update_user: поля применяются
    await c.update_user(old_password=pw, expires_at=now + 7200, total_gb=100, max_devices=3)
    info = await c.get_user_info(pw)
    assert info["expires_at"] == now + 7200
    assert info["total_gb"] == 100
    assert info["max_devices"] == 3

    # reset_traffic и delete_user
    await c.reset_traffic(pw)
    await c.delete_user(pw)
    assert await c.get_user_info(pw) is None

    await c.close()
    await c2.close()
    print("PASS wdtt client (login/csrf/add/update/link/relogin/delete)")


async def test_db(base_url):
    import db

    await db.init_db()

    # plans
    await db.upsert_plan("month", "Месяц", 30, 249.0, 100, 2)
    await db.upsert_plan("week", "Неделя", 7, 99.0, 20, 1)
    plan = await db.get_plan("month")
    assert plan["days"] == 30 and plan["traffic_gb"] == 100
    assert [p["id"] for p in await db.list_plans()] == ["month", "week"]

    # servers (URL и креды заглушки — чтобы бот мог реально обращаться к серверу)
    sid = await db.add_server("main", base_url, "admin", "secret", False)
    assert sid == 1
    assert (await db.list_servers())[0]["base_url"].endswith("/wdtt")

    # customers
    now = int(time.time())
    await db.upsert_customer(1, "pw1", now + 86400, "month", sid, username="u1")
    cust = await db.get_customer(1)
    assert cust["wdtt_password"] == "pw1" and cust["username"] == "u1"
    # продление: expires обновляется, username не теряется
    await db.upsert_customer(1, "pw1", now + 9 * 86400, "month", sid)
    assert (await db.get_customer(1))["username"] == "u1"
    assert (await db.get_customer(1))["expires_at"] == now + 9 * 86400

    # истёкшие клиенты + очистка
    await db.upsert_customer(2, "pw2", now - 10, "week", sid)
    expired = await db.get_expired_customers()
    assert [c["telegram_id"] for c in expired] == [2]
    await db.clear_customer(2)
    assert (await db.get_customer(2))["wdtt_password"] is None
    assert await db.get_expired_customers() == []

    # payments: pending -> paid -> fulfilled
    await db.add_payment(1, "u1", "pay-1", "month", "249.00")
    pending = await db.get_pending_payments()
    assert [p["payment_id"] for p in pending] == ["pay-1"]
    await db.set_payment_status("pay-1", "paid", paid_at=12345)
    assert [p["payment_id"] for p in await db.get_pending_payments()] == ["pay-1"]  # paid тоже добираем
    await db.set_payment_status("pay-1", "fulfilled")
    assert await db.get_pending_payments() == []
    await db.set_payment_status("pay-1", "cancelled")  # не должен вернуть в pending
    assert await db.get_pending_payments() == []
    await db.set_payment_status("pay-1", "fulfilled")  # восстанавливаем для stats

    # trials
    assert not await db.has_used_trial(1)
    await db.mark_trial_used(1)
    assert await db.has_used_trial(1)

    # referrals: атомарное начисление
    await db.add_referral(10, 1)
    assert await db.claim_referral_reward(10) == 1
    assert await db.claim_referral_reward(10) is None
    assert await db.get_referrals_count(1) == 1

    # stats
    s = await db.stats()
    assert s["paid"] == 1 and abs(s["revenue"] - 249.0) < 0.01
    assert s["active"] == 1  # только tg 1

    # delete plan и восстановление
    await db.delete_plan("week")
    assert [p["id"] for p in await db.list_plans()] == ["month"]
    await db.upsert_plan("week", "Неделя", 7, 99.0, 20, 1)
    assert [p["id"] for p in await db.list_plans()] == ["month", "week"]

    print("PASS db (plans/servers/customers/payments/trials/referrals/stats)")


async def test_payments_iso():
    from payments import _iso_to_ts

    ts = _iso_to_ts("2026-10-18T12:00:00+03:00")
    assert isinstance(ts, int) and ts > 1_700_000_000
    assert _iso_to_ts(None) is None
    assert _iso_to_ts("garbage") is None
    print("PASS payments._iso_to_ts")


async def test_bot_imports():
    import bot  # noqa: F401 — регистрация хендлеров должна проходить без ошибок
    import webhook

    app = webhook.make_webhook_app("/yookassa", None)
    assert any(r.resource.canonical == "/yookassa" for r in app.router.routes())
    print("PASS bot import + webhook app")


async def test_bot_flow(panel_base_url):
    """Интеграция: оплата -> выдача / продление / восстановление ключа + реферальный бонус."""
    import db
    import bot as botmod

    # подмена ЮKassa на фейк (без сети)
    async def fake_fetch(payment_id):
        return {"id": payment_id, "status": "succeeded", "paid_at": 1700000000,
                "amount": "249.00", "metadata": {}}
    botmod.fetch_payment = fake_fetch

    # подмена Telegram Bot
    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append((chat_id, text))

    botmod.bot = FakeBot()

    # сервер-заглушка в базе (наименее загруженный -> выберется для новых клиентов)
    fake_sid = await db.add_server("fake-panel", panel_base_url, "admin", "secret", False)
    now = int(time.time())

    # пригласивший с активной подпиской; его ключ существует в панели
    await db.upsert_customer(222, "pw-inv", now + 86400, "month", fake_sid, username="inviter")
    users["pw-inv"] = {"password": "pw***", "password_key": "pw-inv", "comment": "",
                       "expires_at": now + 86400, "total_gb": 50, "max_devices": 1,
                       "active": True, "link": "wdtt://inv"}
    inviter_exp_before = now + 86400

    # реферальная связь до первой оплаты друга
    await db.add_referral(111, 222)

    # 1) первая оплата друга: новый ключ в панели + бонус пригласившему
    await db.add_payment(111, None, "pay-i1", "month", "249.00")
    await botmod.fulfill_payment("pay-i1")
    assert (await db.get_payment("pay-i1"))["status"] == "fulfilled"
    cust111 = await db.get_customer(111)
    assert cust111["wdtt_password"].startswith("userpass-"), cust111
    assert any(cid == 111 and "wdtt://" in t for cid, t in sent), sent
    inviter = await db.get_customer(222)
    assert inviter["expires_at"] == inviter_exp_before + 3 * 86400
    assert users["pw-inv"]["expires_at"] == inviter_exp_before + 3 * 86400
    assert users["pw-inv"]["total_gb"] == 50       # бонус-трафик 0 -> не изменился
    assert users["pw-inv"]["max_devices"] == 1     # бонус-устройств 0 -> не изменился

    # 2) повторная оплата: тот же ключ, срок += 30 дней (продление)
    pwd1, exp1 = cust111["wdtt_password"], cust111["expires_at"]
    await db.add_payment(111, None, "pay-i2", "month", "249.00")
    await botmod.fulfill_payment("pay-i2")
    cust111b = await db.get_customer(111)
    assert cust111b["wdtt_password"] == pwd1
    assert cust111b["expires_at"] == exp1 + 30 * 86400

    # 3) ключ потерян в панели: создаётся новый
    await db.upsert_customer(333, "ghost", now - 3600, "month", fake_sid)
    await db.add_payment(333, None, "pay-i3", "month", "249.00")
    await botmod.fulfill_payment("pay-i3")
    cust333 = await db.get_customer(333)
    assert cust333["wdtt_password"].startswith("userpass-")
    assert cust333["wdtt_password"] != "ghost"
    print("PASS bot flow (fulfill/renew/recover/referral bonus)")


async def test_webhook_e2e():
    import httpx
    from aiohttp import web
    import webhook

    loop = asyncio.get_running_loop()
    got = loop.create_future()

    async def on_succeeded(payment_id):
        if not got.done():
            got.set_result(payment_id)

    app = webhook.make_webhook_app("/yookassa", on_succeeded)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        base = "http://127.0.0.1:%d/yookassa" % port
        async with httpx.AsyncClient() as h:
            r = await h.post(base, json={
                "type": "notification", "event": "payment.succeeded",
                "object": {"id": "pay-wh1"}})
            assert r.status_code == 200 and r.text == "ok", (r.status_code, r.text)
            pid = await asyncio.wait_for(got, timeout=3)
            assert pid == "pay-wh1"
            r2 = await h.post(base, content=b"not json",
                              headers={"Content-Type": "application/json"})
            assert r2.status_code == 400
    finally:
        await runner.cleanup()
    print("PASS webhook e2e (notification -> callback, bad json -> 400)")


def test_config_and_plans():
    import config

    os.environ["WDTT_VERIFY_SSL"] = "on"
    assert config._as_bool("WDTT_VERIFY_SSL") is True
    os.environ["WDTT_VERIFY_SSL"] = "0"
    assert config._as_bool("WDTT_VERIFY_SSL") is False
    assert config._as_int("NOPE", 7) == 7
    os.environ["POLL_INTERVAL"] = "45"
    assert config._as_int("POLL_INTERVAL", 10) == 45
    assert config._parse_admin_ids("1, 2, x,3") == {1, 2, 3}
    for k, v in (("TELEGRAM_BOT_TOKEN", "t"), ("YOOKASSA_SHOP_ID", "s"),
                 ("YOOKASSA_SECRET_KEY", "k"), ("ADMIN_IDS", "1")):
        os.environ[k] = v
    import importlib
    importlib.reload(config)  # config читает env при импорте — перечитываем
    assert config.validate() == []

    from plans import PLANS, TRIAL, ALL_PLANS_BY_ID
    ids = [p["id"] for p in PLANS]
    assert len(ids) == len(set(ids))
    for p in PLANS:
        assert {"id", "name", "days", "price", "traffic_gb", "max_devices", "description"} <= set(p)
        assert p["days"] > 0 and p["price"] >= 0
    assert TRIAL["id"] in ALL_PLANS_BY_ID
    assert all(i in ALL_PLANS_BY_ID for i in ids)
    print("PASS config helpers + plans defaults")


async def main():
    from aiohttp import web

    runner = web.AppRunner(await make_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    base_url = f"http://127.0.0.1:{port}/wdtt"

    try:
        await test_wdtt_client(base_url)
        await test_db(base_url)
        await test_bot_flow(base_url)
        await test_payments_iso()
        await test_webhook_e2e()
        test_config_and_plans()
        await test_bot_imports()
    finally:
        await runner.cleanup()
    print("ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
