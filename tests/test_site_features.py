"""Тесты: инструкции со скриншотами, автовход на сайт из Telegram, чистка профиля."""
import time

import pytest
from aiohttp import web

import api
import web_app


class _Req:
    def __init__(self, query: dict | None = None):
        self.query = query or {}
        self.cookies = {}

    async def post(self):
        return {}


class _FakeCur:
    lastrowid = 1

    async def fetchone(self):
        return None

    async def fetchall(self):
        return []


class _FakeDB:
    async def execute(self, *a, **kw):
        return _FakeCur()

    async def fetchone(self):
        return None


class _FakeConn:
    async def __aenter__(self):
        return _FakeDB()

    async def __aexit__(self, *a):
        return False


# ---------- инструкции ----------

async def test_instructions_api_returns_screenshots():
    """API отдаёт пошаговые инструкции со скриншотами для Android, iOS и Windows."""
    app = web.Application()
    api.register_v1_routes(app)
    from aiohttp.test_utils import TestClient, TestServer
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/api/v1/instructions")
        assert resp.status == 200
        data = await resp.json()
        keys = [p["key"] for p in data["platforms"]]
        assert "android" in keys and "ios" in keys and "windows" in keys
        win = next(p for p in data["platforms"] if p["key"] == "windows")
        assert win["app_url"] and "pwdtt" in win["app_url"].lower()
        for p in data["platforms"]:
            assert p["steps"], f"у {p['key']} нет шагов"
            for s in p["steps"]:
                if s["img"]:  # у Windows шагов может не быть скриншотов
                    assert s["img"].startswith("/static/instr/")
                assert s["caption"]
    finally:
        await client.close()


def test_instruction_assets_exist():
    """Файлы скриншотов лежат в static/instr/ (иначе картинки не отдаются)."""
    import os
    import instructions as instr
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "instr")
    for p in instr.PLATFORMS.values():
        for img, _cap in p["steps"]:
            if not img:
                continue
            path = os.path.join(base, img)
            assert os.path.exists(path), f"нет файла {path}"
            assert os.path.getsize(path) > 1000


# ---------- автовход на сайт ----------

async def test_login_auto_creates_session(monkeypatch):
    """Ссылка из бота (/login/auto?code=...) сразу выдаёт сессию сайта."""
    async def _noop(*a, **kw):
        return None

    async def _get_req(code):
        return {"code": code, "telegram_id": 424242, "username": "tester",
                "created_at": int(time.time()), "used": 0}

    used = []

    async def _mark(code):
        used.append(code)

    monkeypatch.setattr(web_app, "init_db", _noop)
    monkeypatch.setattr(web_app, "get_login_request", _get_req)
    monkeypatch.setattr(web_app, "mark_login_request_used", _mark)
    monkeypatch.setattr(web_app, "db_conn", lambda: _FakeConn())
    monkeypatch.setattr(web_app, "generate_referral_code", lambda: "refcode")

    resp = await web_app.handle_login_auto(_Req({"code": "abc123"}))
    assert resp.status == 302
    assert resp.headers.get("Location") == "/profile"
    assert used == ["abc123"]  # код одноразовый


async def test_login_auto_rejects_bad_code(monkeypatch):
    """Невалидный/использованный код — 400, сессия не выдаётся."""
    async def _noop(*a, **kw):
        return None

    async def _get_req(code):
        return None

    monkeypatch.setattr(web_app, "init_db", _noop)
    monkeypatch.setattr(web_app, "get_login_request", _get_req)
    resp = await web_app.handle_login_auto(_Req({"code": "nope"}))
    assert resp.status == 400


async def test_login_auto_rejects_expired(monkeypatch):
    """Старая ссылка (больше 10 минут) не пускает на сайт."""
    async def _noop(*a, **kw):
        return None

    async def _get_req(code):
        return {"code": code, "telegram_id": 1, "username": None,
                "created_at": int(time.time()) - 3600, "used": 0}

    monkeypatch.setattr(web_app, "init_db", _noop)
    monkeypatch.setattr(web_app, "get_login_request", _get_req)
    resp = await web_app.handle_login_auto(_Req({"code": "old"}))
    assert resp.status == 400


# ---------- профиль: без истории платежей и ссылок ----------

async def test_profile_has_no_payment_history(monkeypatch):
    """В профиле сайта нет истории платежей, строки «На главную | Выход» и автопродления."""
    async def _noop(*a, **kw):
        return None

    async def _get_customer(sub_id):
        return None

    async def _ref_count(tg_id):
        return 0

    async def _site_ref_count(uid):
        return 0

    monkeypatch.setattr(web_app, "_get_site_session", lambda request: {
        "site_user_id": 1, "telegram_id": 424242, "email": None, "username": "u", "created": time.time()})
    monkeypatch.setattr(web_app, "init_db", _noop)
    monkeypatch.setattr(web_app, "db_conn", lambda: _FakeConn())
    monkeypatch.setattr(web_app, "get_customer", _get_customer)
    monkeypatch.setattr(web_app, "get_referrals_count", _ref_count)
    monkeypatch.setattr(web_app, "get_site_referrals_count", _site_ref_count)

    resp = await web_app.handle_profile(_Req())
    assert resp.status == 200
    assert "История платежей" not in resp.text
    assert "На главную</a> |" not in resp.text
    assert "Автопродление" not in resp.text


# ---------- страница инструкции на сайте ----------

async def test_site_instruction_lists_devices():
    """«Инструкция» на сайте — выбор устройства (как в Mini App) + кнопки всех платформ."""
    resp = await web_app.handle_instruction(_Req())
    assert resp.status == 200
    for device in ("android", "ios", "windows"):
        assert f"/instruction/{device}" in resp.text
    assert "PWDTT" in resp.text
    assert "🪟" in resp.text
    assert "Открыть бота" not in resp.text


async def test_site_instruction_device_has_screenshots():
    """Страница устройства: скриншоты, ссылка на приложение и кнопка «Назад»."""
    class _ReqDevice(_Req):
        def __init__(self, device: str):
            super().__init__()
            self.match_info = {"device": device}

    resp = await web_app.handle_instruction_device(_ReqDevice("android"))
    assert resp.status == 200
    assert "/static/instr/" in resp.text
    assert "/instruction" in resp.text  # кнопка «Назад к выбору устройства»
    assert "Открыть бота" not in resp.text

    resp_win = await web_app.handle_instruction_device(_ReqDevice("windows"))
    assert resp_win.status == 200
    assert "PWDTT" in resp_win.text
    assert "windows" in resp_win.text.lower()


async def test_site_instruction_unknown_device_redirects():
    """Неизвестное устройство → редирект на общий экран выбора."""
    class _ReqDevice(_Req):
        def __init__(self, device: str):
            super().__init__()
            self.match_info = {"device": device}

    try:
        await web_app.handle_instruction_device(_ReqDevice("tv"))
    except web.HTTPFound as e:
        assert e.location == "/instruction"
    else:
        raise AssertionError("ожидался редирект на /instruction")


async def test_site_nav_has_instruction_tab():
    """В шапке сайта есть вкладки-разделы, включая «Инструкция» (как в Mini App)."""
    resp = await web_app.handle_index(_Req())
    assert resp.status == 200
    assert 'class="nav"' in resp.text
    assert "Инструкция" in resp.text
    assert 'href="/instruction"' in resp.text

