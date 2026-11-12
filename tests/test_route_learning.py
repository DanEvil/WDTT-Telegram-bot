"""Тесты запоминания лучших маршрутов."""
import sys
from pathlib import Path

import pytest

WIN = Path(__file__).resolve().parents[1] / "Windows"
sys.path.insert(0, str(WIN))

import bypass as BP  # noqa: E402
import route_learning as RL  # noqa: E402


class FakeDRM:
    def __init__(self, direct=None, tunnel=None):
        self.cfg = {"enabled": True, "domains": list(direct or []),
                    "ips": [], "apps": []}
        self.tcfg = {"enabled": True, "domains": list(tunnel or []),
                     "ips": [], "apps": []}
        self.saved = 0

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
        self.saved += 1


@pytest.fixture(autouse=True)
def no_zapret(monkeypatch):
    """Изоляция от реального zapret на машине разработчика."""
    monkeypatch.setattr(BP, "zapret_covers", lambda d: False)
    monkeypatch.setattr(BP, "zapret_add_host", lambda d: (True, f"{d} добавлен"))
    monkeypatch.setattr(BP, "zapret_remove_host", lambda d: (True, f"{d} удалён"))
    monkeypatch.setattr(BP, "zapret_restart", lambda: (True, "перезапущен"))


def test_learner_defaults():
    lr = RL.RouteLearner({})
    assert lr.cfg["enabled"] is True
    assert lr.cfg["auto_apply"] is True
    assert lr.cfg["min_samples"] == 3
    assert lr.cfg["routes"] == {}


def test_record_and_stats():
    lr = RL.RouteLearner({})
    lr.record("a.example", RL.ROUTE_DIRECT, 100, True)
    lr.record("a.example", RL.ROUTE_DIRECT, 200, True)
    st = lr.stats("a.example")["direct"]
    assert st["avg_ms"] == 150
    assert st["samples"] == 2
    assert st["ok"] == 2


def test_record_ignores_unmeasured_route():
    lr = RL.RouteLearner({})
    lr.record("a.example", RL.ROUTE_TUNNEL, 100, True)
    assert lr.stats("a.example") == {}


def test_record_failure_tracked():
    lr = RL.RouteLearner({})
    lr.record("a.example", RL.ROUTE_DIRECT, None, False, "timeout")
    st = lr.stats("a.example")["direct"]
    assert st["samples"] == 0
    assert st["fail"] == 1
    assert st["last_error"] == "timeout"


def test_samples_capped():
    lr = RL.RouteLearner({})
    for i in range(RL.MAX_SAMPLES + 10):
        lr.record("a.example", RL.ROUTE_DIRECT, 100 + i, True)
    st = lr.stats("a.example")["direct"]
    assert st["samples"] == RL.MAX_SAMPLES


def test_best_route_picks_faster():
    lr = RL.RouteLearner({"min_samples": 1})
    for _ in range(3):
        lr.record("a.example", RL.ROUTE_DIRECT, 100, True)
        lr.record("a.example", RL.ROUTE_VPN, 900, True)
    route, avg = lr.best_route("a.example")
    assert route == RL.ROUTE_DIRECT
    assert avg == 100


def test_best_route_ignores_failed():
    lr = RL.RouteLearner({"min_samples": 1})
    for _ in range(3):
        lr.record("a.example", RL.ROUTE_DIRECT, None, False, "timeout")
        lr.record("a.example", RL.ROUTE_VPN, 250, True)
    route, _ = lr.best_route("a.example")
    assert route == RL.ROUTE_VPN


def test_best_route_needs_evidence():
    """Один замер при большой разнице — достаточно; при близкой — нет."""
    lr = RL.RouteLearner({"min_samples": 3})
    lr.record("a.example", RL.ROUTE_DIRECT, 100, True)
    lr.record("a.example", RL.ROUTE_VPN, 5000, True)
    assert lr.best_route("a.example")[0] == RL.ROUTE_DIRECT

    lr2 = RL.RouteLearner({"min_samples": 3})
    lr2.record("b.example", RL.ROUTE_DIRECT, 100, True)
    lr2.record("b.example", RL.ROUTE_VPN, 120, True)
    assert lr2.best_route("b.example")[0] is None


def test_recommendation_confidence():
    lr = RL.RouteLearner({"min_samples": 3})
    for _ in range(3):
        lr.record("a.example", RL.ROUTE_DIRECT, 100, True)
        lr.record("a.example", RL.ROUTE_VPN, 900, True)
    rec = lr.recommendation("a.example")
    assert rec["route"] == RL.ROUTE_DIRECT
    assert rec["confident"] is True
    assert "Прямое" in rec["reason"]


def test_apply_direct_adds_to_direct():
    drm = FakeDRM()
    lr = RL.RouteLearner({})
    ok, msg = lr.apply("a.example", RL.ROUTE_DIRECT, drm=drm)
    assert ok
    assert "a.example" in drm.cfg["domains"]
    assert drm.saved == 1


def test_apply_vpn_clears_direct():
    drm = FakeDRM(direct=["a.example"])
    lr = RL.RouteLearner({})
    ok, msg = lr.apply("a.example", RL.ROUTE_VPN, drm=drm)
    assert ok
    assert "a.example" not in drm.cfg["domains"]
    assert "туннель" in msg


def test_apply_zapret_keeps_direct_lists():
    """zapret — обход DPI, а не путь трафика: списки маршрутов не трогаем."""
    drm = FakeDRM(direct=["a.example"])
    lr = RL.RouteLearner({})
    ok, msg = lr.apply("a.example", RL.ROUTE_ZAPRET, drm=drm)
    assert ok
    assert "a.example" in drm.cfg["domains"]


def test_apply_all_skips_unconfident(monkeypatch):
    drm = FakeDRM()
    lr = RL.RouteLearner({"min_samples": 5})
    lr.record("a.example", RL.ROUTE_DIRECT, 100, True)
    results = lr.apply_all(drm=drm)
    assert results == []
    assert drm.cfg["domains"] == []


def test_apply_all_applies_confident():
    drm = FakeDRM()
    lr = RL.RouteLearner({"min_samples": 2})
    for _ in range(3):
        lr.record("a.example", RL.ROUTE_DIRECT, 100, True)
        lr.record("a.example", RL.ROUTE_VPN, 900, True)
    results = lr.apply_all(drm=drm)
    assert len(results) == 1
    assert results[0]["ok"]
    assert "a.example" in drm.cfg["domains"]


def test_clear_domain():
    lr = RL.RouteLearner({})
    lr.record("a.example", RL.ROUTE_DIRECT, 100, True)
    lr.record("b.example", RL.ROUTE_DIRECT, 100, True)
    lr.clear("a.example")
    assert lr.domains() == ["b.example"]
    lr.clear()
    assert lr.domains() == []


def test_detect_current_route_disconnected_is_direct():
    drm = FakeDRM()
    assert RL.detect_current_route("x.example", drm, False) == RL.ROUTE_DIRECT


def test_detect_current_route_connected_is_vpn():
    drm = FakeDRM()
    assert RL.detect_current_route("x.example", drm, True) == RL.ROUTE_VPN


def test_detect_current_route_direct_list():
    drm = FakeDRM(direct=["x.example"])
    assert RL.detect_current_route("x.example", drm, True) == RL.ROUTE_DIRECT


def test_detect_current_route_tunnel_list_is_vpn_path():
    """Принудительный туннель физически = путь VPN."""
    drm = FakeDRM(tunnel=["x.example"])
    assert RL.detect_current_route("x.example", drm, False) == RL.ROUTE_VPN


def test_detect_current_route_zapret(monkeypatch):
    monkeypatch.setattr(BP, "zapret_covers", lambda d: d == "x.example")
    drm = FakeDRM()
    assert RL.detect_current_route("x.example", drm, True) == RL.ROUTE_ZAPRET


def test_normalize_and_host():
    assert RL._host_of("https://www.example.com/path") == "www.example.com"
