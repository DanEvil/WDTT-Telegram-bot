"""Тесты админ-панели сайта: форма автоустановки сервера по SSH и выбор least-loaded сервера."""
import asyncio
import time

import pytest
from aiohttp import web

import web_app
import server_setup
from config import ADMIN_IDS

ADMIN_TG = next(iter(ADMIN_IDS)) if ADMIN_IDS else 1035529538


def _session(telegram_id: int) -> dict:
    return {"site_user_id": 1, "telegram_id": telegram_id, "email": "a@example.com", "created": time.time()}


class _Req:
    def __init__(self, data: dict):
        self._data = data
        self.cookies = {}
        self.query = {}

    async def post(self):
        return self._data


def _mock_admin_db(monkeypatch):
    async def _noop(*a, **kw):
        return None

    async def _stats():
        return {"customers": 0, "active": 0, "paid": 0, "revenue": 0}

    async def _servers():
        return []

    async def _counts():
        return {}

    monkeypatch.setattr(web_app, "init_db", _noop)
    monkeypatch.setattr(web_app, "stats", _stats)
    monkeypatch.setattr(web_app, "list_servers", _servers)
    monkeypatch.setattr(web_app, "server_active_counts", _counts)


async def test_admin_page_shows_install_form(monkeypatch):
    """Админ видит форму /admin/server/install и таблицу серверов с лимитами."""
    _mock_admin_db(monkeypatch)
    monkeypatch.setattr(web_app, "_get_site_session", lambda request: _session(ADMIN_TG))
    resp = await web_app.handle_admin(_Req({}))
    assert resp.status == 200
    assert "/admin/server/install" in resp.text
    assert "Автоустановка" in resp.text or "автоустановка" in resp.text


async def test_admin_page_forbidden_for_non_admin(monkeypatch):
    _mock_admin_db(monkeypatch)
    monkeypatch.setattr(web_app, "_get_site_session", lambda request: _session(999999))
    resp = await web_app.handle_admin(_Req({}))
    assert resp.status == 403


async def test_install_requires_admin(monkeypatch):
    monkeypatch.setattr(web_app, "_get_site_session", lambda request: _session(999999))
    resp = await web_app.handle_admin_install_server(_Req({
        "name": "de2", "host": "1.2.3.4", "ssh_port": "22", "ssh_user": "root", "ssh_password": "x",
    }))
    assert resp.status == 403


async def test_install_validates_fields(monkeypatch):
    monkeypatch.setattr(web_app, "_get_site_session", lambda request: _session(ADMIN_TG))
    resp = await web_app.handle_admin_install_server(_Req({"name": "", "host": "", "ssh_port": "abc"}))
    assert resp.status == 400


async def test_install_starts_background_deploy(monkeypatch):
    """Валидный POST запускает фоновую установку и сразу возвращает редирект."""
    monkeypatch.setattr(web_app, "_get_site_session", lambda request: _session(ADMIN_TG))

    deployed = {}

    async def fake_deploy(**kwargs):
        deployed.update(kwargs)
        return {"id": 7, "base_url": "https://1.2.3.4:2860/wdtt",
                "panel_username": "adm_x", "panel_password": "p"}

    monkeypatch.setattr(server_setup, "deploy_server", fake_deploy)

    async def _no_vk():
        return []

    import db as dbmod
    monkeypatch.setattr(dbmod, "list_vk_accounts", _no_vk)

    web_app._installs.clear()
    with pytest.raises(web.HTTPFound):
        await web_app.handle_admin_install_server(_Req({
            "name": "de2", "host": "1.2.3.4", "ssh_port": "22", "ssh_user": "root", "ssh_password": "secret",
        }))
    assert web_app._installs, "запись об установке должна появиться сразу"
    rec = list(web_app._installs.values())[-1]
    assert rec["status"] == "running" and rec["host"] == "1.2.3.4"
    # дождаться завершения фоновой задачи
    for _ in range(50):
        await asyncio.sleep(0.05)
        if rec["status"] != "running":
            break
    assert rec["status"] == "done"
    assert "#7" in rec["detail"]
    assert deployed["host"] == "1.2.3.4" and deployed["ssh_password"] == "secret"
    assert rec.get("ssh_password") is None


async def test_pick_least_loaded_server(monkeypatch):
    """least-loaded: выбирается сервер с меньшим числом активных клиентов."""
    from server_select import pick_least_loaded_server_id

    async def _servers():
        return [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]

    async def _counts():
        return {1: 50, 2: 3}

    import server_select
    monkeypatch.setattr(server_select, "list_servers", _servers)
    monkeypatch.setattr(server_select, "server_active_counts", _counts)
    assert await pick_least_loaded_server_id() == 2


async def test_pick_least_loaded_returns_none_when_full(monkeypatch):
    """Все серверы на лимите — None (админ должен добавить сервер)."""
    from server_select import pick_least_loaded_server_id
    import server_select
    import config

    async def _servers():
        return [{"id": 1, "name": "a"}]

    async def _counts():
        return {1: config.SERVER_MAX_USERS}

    monkeypatch.setattr(server_select, "list_servers", _servers)
    monkeypatch.setattr(server_select, "server_active_counts", _counts)
    assert await pick_least_loaded_server_id() is None
