"""Тесты per-user VLESS-доступа: генерация ссылки, модуль управления, токен."""
import asyncio
import json
import os
import sys

sys.path.insert(0, r"C:\Users\nenas\WDTT-Telegram-bot")
os.chdir(r"C:\Users\nenas\WDTT-Telegram-bot")

import pytest

import config
import vless_server


# ---------- _build_vless_url с персональным UUID ----------

def test_build_vless_url_uses_personal_uuid(monkeypatch):
    import bot
    monkeypatch.setattr(config, "VLESS_ENABLED", True)
    monkeypatch.setattr(config, "VLESS_SERVER", "1.2.3.4")
    monkeypatch.setattr(config, "VLESS_PORT", 443)
    monkeypatch.setattr(config, "VLESS_UUID", "shared-uuid")
    monkeypatch.setattr(config, "VLESS_SECURITY", "reality")
    monkeypatch.setattr(config, "VLESS_PROTOCOL", "tcp")
    monkeypatch.setattr(config, "VLESS_PUBLIC_KEY", "PBK")
    monkeypatch.setattr(config, "VLESS_SHORT_ID", "SID")
    monkeypatch.setattr(config, "VLESS_SNI", "www.samsung.com")
    monkeypatch.setattr(config, "VLESS_FLOW", "xtls-rprx-vision")

    url = bot._build_vless_url(vless_uuid="personal-uuid")
    assert url is not None
    assert url.startswith("vless://personal-uuid@1.2.3.4:443?")
    assert "security=reality" in url
    assert "pbk=PBK" in url and "sid=SID" in url
    assert "sni=www.samsung.com" in url
    assert "flow=xtls-rprx-vision" in url

    shared = bot._build_vless_url()
    assert shared is not None
    assert shared.startswith("vless://shared-uuid@")


def test_build_vless_url_without_uuid_returns_none(monkeypatch):
    import bot
    monkeypatch.setattr(config, "VLESS_ENABLED", True)
    monkeypatch.setattr(config, "VLESS_SERVER", "1.2.3.4")
    monkeypatch.setattr(config, "VLESS_UUID", "")
    assert bot._build_vless_url() is None


# ---------- vless_server: формат команд и JSON ----------

def test_email_for():
    assert vless_server.email_for(123456) == "tg123456"


def test_inbound_json_structure(monkeypatch):
    monkeypatch.setattr(config, "VLESS_INBOUND_TAG", "vless-in")
    monkeypatch.setattr(config, "VLESS_INBOUND_PORT", 443)
    monkeypatch.setattr(config, "VLESS_FLOW", "xtls-rprx-vision")
    data = json.loads(vless_server._inbound_json([vless_server._client("uuid-1", "tg42")]))
    inb = data["inbounds"][0]
    assert inb["tag"] == "vless-in"
    assert inb["listen"] == "0.0.0.0"
    assert inb["port"] == 443
    assert inb["protocol"] == "vless"
    client = inb["settings"]["clients"][0]
    assert client == {"id": "uuid-1", "email": "tg42", "flow": "xtls-rprx-vision"}


def test_enabled_requires_ssh(monkeypatch):
    monkeypatch.setattr(config, "VLESS_PER_USER", True)
    monkeypatch.setattr(config, "VLESS_SSH_HOST", "")
    monkeypatch.setattr(config, "VLESS_SSH_PASSWORD", "")
    assert vless_server.enabled() is False

    monkeypatch.setattr(config, "VLESS_SSH_HOST", "1.2.3.4")
    monkeypatch.setattr(config, "VLESS_SSH_PASSWORD", "pass")
    assert vless_server.enabled() is True

    monkeypatch.setattr(config, "VLESS_PER_USER", False)
    assert vless_server.enabled() is False


def test_add_user_builds_expected_command(monkeypatch):
    calls = []

    async def fake_ssh(cmd, timeout=30):
        calls.append(cmd)
        if "cat " in cmd:
            return 0, "[]", ""
        return 0, "Added 1 user(s) in total.", ""

    monkeypatch.setattr(config, "VLESS_PER_USER", True)
    monkeypatch.setattr(config, "VLESS_SSH_HOST", "1.2.3.4")
    monkeypatch.setattr(config, "VLESS_SSH_PASSWORD", "pass")
    monkeypatch.setattr(config, "VLESS_XRAY_BIN", "/usr/local/xray-vless/bin/xray")
    monkeypatch.setattr(config, "VLESS_API_SERVER", "127.0.0.1:62790")
    monkeypatch.setattr(config, "VLESS_INBOUND_TAG", "vless-in")
    monkeypatch.setattr(config, "VLESS_INBOUND_PORT", 443)
    monkeypatch.setattr(config, "VLESS_FLOW", "xtls-rprx-vision")
    monkeypatch.setattr(config, "VLESS_CLIENTS_FILE", "/etc/xray-vless/users.json")
    monkeypatch.setattr(vless_server, "_ssh", fake_ssh)

    ok = asyncio.run(vless_server.add_user("uuid-1", "tg42"))
    assert ok is True
    adu = [c for c in calls if "api adu" in c][0]
    assert "--server=127.0.0.1:62790" in adu
    # JSON передаётся base64, чтобы не сломать shell-экранирование
    assert "base64 -d" in adu
    import base64
    payload = adu.split("echo ", 1)[1].split(" |", 1)[0]
    data = json.loads(base64.b64decode(payload).decode())
    assert data["inbounds"][0]["settings"]["clients"][0]["id"] == "uuid-1"
    # список клиентов должен быть сохранён на диск (переживает рестарт xray)
    saved = [c for c in calls if "base64 -d > /etc/xray-vless/users.json.tmp" in c]
    assert saved, "файл клиентов не сохранён"
    spayload = saved[0].split("echo ", 1)[1].split(" |", 1)[0]
    clients = json.loads(base64.b64decode(spayload).decode())
    assert clients[0]["email"] == "tg42"


def test_read_clients_parses_file(monkeypatch):
    async def fake_ssh(cmd, timeout=30):
        return 0, json.dumps([{"id": "u1", "email": "tg1"}]), ""

    monkeypatch.setattr(config, "VLESS_PER_USER", True)
    monkeypatch.setattr(config, "VLESS_SSH_HOST", "1.2.3.4")
    monkeypatch.setattr(config, "VLESS_SSH_PASSWORD", "pass")
    monkeypatch.setattr(config, "VLESS_CLIENTS_FILE", "/etc/xray-vless/users.json")
    monkeypatch.setattr(vless_server, "_ssh", fake_ssh)

    clients = asyncio.run(vless_server.read_clients())
    assert clients == [{"id": "u1", "email": "tg1"}]


def test_remove_user_handles_not_found(monkeypatch):
    async def fake_ssh(cmd, timeout=30):
        return 0, "rpc error: User tg42 not found.\nRemoved 0 user(s) in total.", ""

    monkeypatch.setattr(config, "VLESS_PER_USER", True)
    monkeypatch.setattr(config, "VLESS_SSH_HOST", "1.2.3.4")
    monkeypatch.setattr(config, "VLESS_SSH_PASSWORD", "pass")
    monkeypatch.setattr(config, "VLESS_XRAY_BIN", "/usr/local/xray-vless/bin/xray")
    monkeypatch.setattr(config, "VLESS_API_SERVER", "127.0.0.1:62790")
    monkeypatch.setattr(config, "VLESS_INBOUND_TAG", "vless-in")
    monkeypatch.setattr(vless_server, "_ssh", fake_ssh)

    # «уже нет» считаем успехом
    assert asyncio.run(vless_server.remove_user("tg42")) is True


def test_list_emails_parses_json(monkeypatch):
    async def fake_ssh(cmd, timeout=30):
        return 0, json.dumps({"users": [{"email": "tg1"}, {"email": "tg2"}]}), ""

    monkeypatch.setattr(config, "VLESS_PER_USER", True)
    monkeypatch.setattr(config, "VLESS_SSH_HOST", "1.2.3.4")
    monkeypatch.setattr(config, "VLESS_SSH_PASSWORD", "pass")
    monkeypatch.setattr(vless_server, "_ssh", fake_ssh)

    assert asyncio.run(vless_server.list_emails()) == ["tg1", "tg2"]


# ---------- токен vk-hash-service ----------

def test_vk_hash_token_required(monkeypatch):
    import importlib
    import vk_hash_service
    importlib.reload(vk_hash_service)

    class Req:
        def __init__(self, headers):
            self.headers = headers

    monkeypatch.setattr(vk_hash_service, "TOKEN", "secret")
    assert vk_hash_service._token_ok(Req({"X-VK-Hash-Token": "secret"})) is True
    assert vk_hash_service._token_ok(Req({"X-VK-Hash-Token": "wrong"})) is False
    assert vk_hash_service._token_ok(Req({})) is False

    monkeypatch.setattr(vk_hash_service, "TOKEN", "")
    assert vk_hash_service._token_ok(Req({})) is True
