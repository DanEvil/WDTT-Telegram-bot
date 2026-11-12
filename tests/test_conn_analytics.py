"""Тесты аналитики типа соединения (включая zapret как тип маршрута)."""
import sys
from pathlib import Path

import pytest

WIN = Path(__file__).resolve().parents[1] / "Windows"
sys.path.insert(0, str(WIN))

import bypass as BP  # noqa: E402
import conn_analytics as CA  # noqa: E402


class FakeDRM:
    """Заглушка DirectRoutesManager с управляемыми списками маршрутов."""

    def __init__(self, direct=None, tunnel=None, direct_on=True, tunnel_on=True):
        self.cfg = {"enabled": direct_on, "domains": list(direct or []),
                    "ips": [], "apps": []}
        self.tcfg = {"enabled": tunnel_on, "domains": list(tunnel or []),
                     "ips": [], "apps": []}

    def is_direct_enabled(self):
        return bool(self.cfg.get("enabled"))

    def is_tunnel_enabled(self):
        return bool(self.tcfg.get("enabled"))

    def direct_add(self, kind, value):
        if value not in self.cfg[kind]:
            self.cfg[kind].append(value)

    def tunnel_add(self, kind, value):
        if value not in self.tcfg[kind]:
            self.tcfg[kind].append(value)

    def direct_remove(self, kind, value):
        if value in self.cfg[kind]:
            self.cfg[kind].remove(value)

    def tunnel_remove(self, kind, value):
        if value in self.tcfg[kind]:
            self.tcfg[kind].remove(value)

    def save(self):
        pass


def test_type_labels_cover_all_types():
    for kind in CA.TYPE_ORDER:
        assert kind in CA.TYPE_LABELS


def test_detect_direct(monkeypatch):
    monkeypatch.setattr(BP, "zapret_covers", lambda d: False)
    an = CA.ConnectionAnalyzer(FakeDRM(direct=["yandex.ru"]))
    assert an.detect("yandex.ru") == "direct"
    assert an.detect("www.yandex.ru") == "direct"  # поддомен


def test_detect_tunnel(monkeypatch):
    monkeypatch.setattr(BP, "zapret_covers", lambda d: False)
    an = CA.ConnectionAnalyzer(FakeDRM(tunnel=["telegram.org"]))
    assert an.detect("telegram.org") == "tunnel"


def test_detect_vpn_default(monkeypatch):
    monkeypatch.setattr(BP, "zapret_covers", lambda d: False)
    an = CA.ConnectionAnalyzer(FakeDRM())
    assert an.detect("google.com") == "vpn"


def test_detect_zapret(monkeypatch):
    monkeypatch.setattr(BP, "zapret_covers", lambda d: d == "blocked.example")
    an = CA.ConnectionAnalyzer(FakeDRM())
    assert an.detect("blocked.example") == "zapret"
    assert an.detect("google.com") == "vpn"


def test_detect_blocked(monkeypatch):
    monkeypatch.setattr(BP, "zapret_covers", lambda d: False)
    an = CA.ConnectionAnalyzer(FakeDRM())
    assert an.detect("dead.example", blocked=True) == "blocked"


def test_disabled_direct_falls_back(monkeypatch):
    monkeypatch.setattr(BP, "zapret_covers", lambda d: False)
    an = CA.ConnectionAnalyzer(FakeDRM(direct=["yandex.ru"], direct_on=False))
    assert an.detect("yandex.ru") == "vpn"


def test_summary_counts(monkeypatch):
    monkeypatch.setattr(BP, "zapret_covers", lambda d: d == "z.example")
    monkeypatch.setattr(BP, "zapret_state", lambda: {"installed": True, "running": False,
                                                    "hosts_count": 1, "hosts": []})
    an = CA.ConnectionAnalyzer(FakeDRM(direct=["d.example"], tunnel=["t.example"]))
    s = an.summary(["d.example", "t.example", "z.example", "v.example"])
    assert s["counts"]["direct"] == 1
    assert s["counts"]["tunnel"] == 1
    assert s["counts"]["zapret"] == 1
    assert s["counts"]["vpn"] == 1
    assert len(s["rows"]) == 4
    assert s["zapret"]["installed"] is True


def test_summary_marks_blocked(monkeypatch):
    monkeypatch.setattr(BP, "zapret_covers", lambda d: False)
    an = CA.ConnectionAnalyzer(FakeDRM())
    s = an.summary(["a.example", "b.example"], blocked=["b.example"])
    kinds = {r["domain"]: r["type"] for r in s["rows"]}
    assert kinds["a.example"] == "vpn"
    assert kinds["b.example"] == "blocked"


def test_set_type_direct_and_tunnel(monkeypatch):
    monkeypatch.setattr(BP, "zapret_covers", lambda d: False)
    an = CA.ConnectionAnalyzer(FakeDRM())
    ok, msg = an.set_type("x.example", "direct")
    assert ok and "x.example" in msg
    assert an.drm.cfg["domains"] == ["x.example"]

    ok, _ = an.set_type("x.example", "tunnel")
    assert ok
    assert "x.example" not in an.drm.cfg["domains"]  # прежнее назначение снято
    assert "x.example" in an.drm.tcfg["domains"]


def test_set_type_zapret(monkeypatch):
    added = []
    monkeypatch.setattr(BP, "zapret_covers", lambda d: False)
    monkeypatch.setattr(BP, "zapret_add_host",
                        lambda d: (added.append(d), (True, "ok"))[1])
    monkeypatch.setattr(BP, "zapret_restart", lambda: (True, "перезапущен"))
    an = CA.ConnectionAnalyzer(FakeDRM())
    ok, msg = an.set_type("z.example", "zapret")
    assert ok and "zapret" in msg
    assert added == ["z.example"]


def test_set_type_vpn_clears(monkeypatch):
    removed = []
    monkeypatch.setattr(BP, "zapret_covers", lambda d: False)
    monkeypatch.setattr(BP, "zapret_remove_host",
                        lambda d: (removed.append(d), (True, "ok"))[1])
    an = CA.ConnectionAnalyzer(FakeDRM(direct=["v.example"], tunnel=["v.example"]))
    ok, _ = an.set_type("v.example", "vpn")
    assert ok
    assert removed == ["v.example"]
    assert an.drm.cfg["domains"] == []
    assert an.drm.tcfg["domains"] == []


def test_set_type_unknown(monkeypatch):
    monkeypatch.setattr(BP, "zapret_covers", lambda d: False)
    an = CA.ConnectionAnalyzer(FakeDRM())
    ok, msg = an.set_type("x.example", "unknown")
    assert not ok and "Неизвестный" in msg


def test_domain_in_list_subdomain():
    assert BP.domain_in_list("a.b.example", ["example"])
    assert BP.domain_in_list("example.com", ["*.example.com"])
    assert not BP.domain_in_list("other.org", ["example.com"])


def test_normalize_domain():
    assert BP.normalize_domain("https://www.google.com/path") == "www.google.com"
    assert BP.normalize_domain("HTTP://Example.COM:443") == "example.com"
