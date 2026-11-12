
"""Тесты репозитория запрета: каталог техник блокировки и классификация."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import block_methods as bm


def test_catalog_not_empty():
    catalog = bm.get_catalog()
    assert len(catalog) >= 8
    ids = [m["id"] for m in catalog]
    assert len(ids) == len(set(ids)), "id техник должны быть уникальными"


def test_catalog_structure():
    for method in bm.get_catalog():
        assert method["id"] and method["title"] and method["description"]
        assert method["kind"] in {"dns", "network", "dpi", "throttle"}
        assert method["remedies"], f"у техники {method['id']} нет обходов"
        for remedy in method["remedies"]:
            assert remedy["id"] and remedy["title"] and remedy["detail"]
            assert remedy["kind"] in {"zapret", "direct", "tunnel", "browser", "dns", "vpn"}


def test_get_method():
    m = bm.get_method("sni_filter")
    assert m is not None
    assert m["id"] == "sni_filter"
    assert bm.get_method("no_such_method") is None


def test_classify_dns_failure():
    verdict = bm.classify({"host": "x.example", "dns_ok": False, "error": "dns: gaierror"})
    assert verdict["method_id"] == "dns_blackhole"
    assert verdict["severity"] == "blocked"


def test_classify_tcp_failure():
    verdict = bm.classify({"host": "x.example", "dns_ok": True, "tcp_ok": False,
                           "error": "tcp: timeout"})
    assert verdict["method_id"] == "ip_block"
    assert verdict["severity"] == "blocked"


def test_classify_tls_failure_is_sni():
    verdict = bm.classify({"host": "x.example", "dns_ok": True, "tcp_ok": True,
                           "tls_ok": False, "error": "tls: timeout"})
    assert verdict["method_id"] == "sni_filter"


def test_classify_reset_is_rst():
    verdict = bm.classify({"host": "x.example", "dns_ok": True, "tcp_ok": True,
                           "tls_ok": False, "error": "tls: ConnectionResetError"})
    assert verdict["method_id"] == "rst_injection"


def test_classify_throttle():
    verdict = bm.classify({"host": "x.example", "dns_ok": True, "tcp_ok": True,
                           "tls_ok": True, "http_ok": True, "http_ms": 9000})
    assert verdict["method_id"] == "tcp_throttle"
    assert verdict["severity"] == "degraded"


def test_classify_ok():
    verdict = bm.classify({"host": "x.example", "dns_ok": True, "tcp_ok": True,
                           "tls_ok": True, "http_ok": True, "http_ms": 300})
    assert verdict["method_id"] is None
    assert verdict["severity"] == "ok"


def test_diagnose_has_remedies_for_blocked():
    diag = bm.diagnose("https://127.0.0.1:1/", timeout=1.0)
    assert "verdict" in diag and "probe" in diag
    assert diag["host"] == "127.0.0.1"
    if diag["verdict"]["method_id"]:
        assert diag["remedies"]
