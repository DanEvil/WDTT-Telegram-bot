"""Тесты автовхода на сайт из мини-аппа: /tg-login (страница) и /tg-login/do (initData -> cookie-сессия)."""
import hashlib
import hmac
import time
from urllib.parse import urlencode

import pytest
from aiohttp import web

import web_app
from web_app import TELEGRAM_TOKEN


def _make_init_data(user: dict, token: str) -> str:
    """Собирает initData так, как это делает Telegram Mini Apps."""
    fields = {
        "auth_date": str(int(time.time())),
        "user": __import__("json").dumps(user, separators=(",", ":")),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(token.encode(), b"WebAppData", hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class _Req:
    def __init__(self, body: dict | None = None, cookies: dict | None = None):
        self._body = body or {}
        self.cookies = cookies or {}
        self.query = {}

    async def json(self):
        return self._body


TG_USER = 555000111


async def test_tg_login_do_creates_session():
    """Валидный initData -> 200, cookie session_id, site_user создан, telegram_id в сессии."""
    init_data = _make_init_data({"id": TG_USER, "first_name": "Тест", "username": "tester"}, TELEGRAM_TOKEN or "test-token")
    resp = await web_app.handle_tg_login_do(_Req({"initData": init_data}))
    assert resp.status == 200
    d = __import__("json").loads(resp.body)
    assert d["ok"] and d["redirect"] == "/profile"
    cookie = resp.cookies.get("session_id")
    assert cookie and cookie.value in web_app.SESSIONS
    sess = web_app.SESSIONS[cookie.value]
    assert sess["telegram_id"] == TG_USER

    # site_user создан и привязан к telegram_id
    import db as dbmod
    async with dbmod.conn() as c:
        cur = await c.execute("SELECT * FROM site_users WHERE telegram_id = ?", (TG_USER,))
        row = await cur.fetchone()
        assert row and row["email"] == f"tg_{TG_USER}@wdtt.local"
        # повторный вход — тот же site_user, без дубля
        resp2 = await web_app.handle_tg_login_do(_Req({"initData": init_data}))
        assert resp2.status == 200
        cur = await c.execute("SELECT COUNT(*) c FROM site_users WHERE telegram_id = ?", (TG_USER,))
        assert (await cur.fetchone())["c"] == 1


async def test_tg_login_do_rejects_bad_init_data():
    resp = await web_app.handle_tg_login_do(_Req({"initData": "hash=deadbeef&user=%7B%22id%22%3A1%7D"}))
    assert resp.status == 403
    resp = await web_app.handle_tg_login_do(_Req({"initData": ""}))
    assert resp.status == 403
    resp = await web_app.handle_tg_login_do(_Req({}))
    assert resp.status == 403
    # чужой токен — тоже отказ
    other = _make_init_data({"id": TG_USER}, "999:OTHER-TOKEN")
    resp = await web_app.handle_tg_login_do(_Req({"initData": other}))
    assert resp.status == 403


async def test_tg_login_page():
    """GET /tg-login без сессии — страница с JS-автовходом; с сессией — редирект на /profile."""
    resp = await web_app.handle_tg_login_page(_Req())
    assert resp.status == 200
    assert "/tg-login/do" in resp.text
    assert "#init" in resp.text or "init=" in resp.text
    # регрессия: location.hash начинается с '#', парсер обязан его срезать,
    # иначе initData из ссылки приложения никогда не достаётся (баг «Нет данных Telegram»)
    assert resp.text is not None and "replace(/^#/" in resp.text

    # с валидной сессией — редирект
    sid = "testsid123"
    web_app.SESSIONS[sid] = {"site_user_id": 1, "telegram_id": TG_USER, "email": None, "created": time.time()}
    try:
        with pytest.raises(web.HTTPFound) as ei:
            await web_app.handle_tg_login_page(_Req(cookies={"session_id": sid}))
        assert ei.value.location == "/profile"
    finally:
        web_app.SESSIONS.pop(sid, None)
