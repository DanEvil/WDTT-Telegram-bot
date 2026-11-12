"""Запоминание лучшего маршрута для каждого сайта.

Приложение не знает заранее, через что конкретный сайт работает быстрее:
одни ресурсы operator'ский DPI душит (нужен zapret или прямой выход),
другие из-за гео-локации не открываются напрямую (нужен туннель).

Модуль копит статистику по каждому домену и для каждого типа маршрута
(прямое / туннель / zapret / обычный VPN), затем выбирает лучший и
применяет его: домен попадает в `direct_routes`, `tunnel_routes` или
в список zapret.

Измерения честные, два источника:

1. **Наблюдения** — замеры во время работы приложения. Маршрут определяется
   по состоянию соединения и спискам: подключено → туннель, отключено →
   прямое соединение, домен в `direct_routes` → прямое, домен в списке
   zapret → zapret. Так накапливаются данные со временем, в разных сетях.

2. **Одноразовое A/B-тестирование** — если на машине есть локальный SOCKS5
   (OpenFlux поднимает `127.0.0.1:1080`), можно замерить оба пути
   одновременно: напрямую и через прокси. Это даёт сравнение в одних
   условиях, без ожидания смены сети.

Хранение — в `config.json`, поэтому настройки переживают перезапуск.
"""

from __future__ import annotations

import os
import socket
import ssl
import time
import urllib.request

try:
    import bypass as BP
except ImportError:  # pragma: no cover
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import bypass as BP

try:
    import conn_analytics as CA
except ImportError:  # pragma: no cover
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import conn_analytics as CA

# Типы маршрутов, между которыми выбирается лучший
ROUTE_DIRECT = "direct"
ROUTE_TUNNEL = "tunnel"
ROUTE_ZAPRET = "zapret"
ROUTE_VPN = "vpn"

ROUTE_LABELS = {
    ROUTE_DIRECT: "Прямое",
    ROUTE_TUNNEL: "Туннель",
    ROUTE_ZAPRET: "Zapret",
    ROUTE_VPN: "VPN",
}

# Типы маршрутов, которые можно назначить домену
ASSIGNABLE = (ROUTE_DIRECT, ROUTE_TUNNEL, ROUTE_ZAPRET, ROUTE_VPN)

# Маршруты, по которым реально измеряется трафик.
# "tunnel" и "vpn" — это один и тот же физический путь (VPN-туннель),
# поэтому замеры идут в счётчик "vpn", а "tunnel" остаётся только
# способом назначения (принудительный маршрут вместо обычного).
MEASURED = (ROUTE_DIRECT, ROUTE_VPN, ROUTE_ZAPRET)

# Кандидаты локального SOCKS5 для A/B-теста
LOCAL_SOCKS_CANDIDATES = (("127.0.0.1", 1080), ("127.0.0.1", 1081))

MAX_SAMPLES = 12  # сколько последних замеров храним на маршрут

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) K.O.Gateway/1.0"


# ─────────────────────────────────────────────────────────────────────────────
# Измерение
# ─────────────────────────────────────────────────────────────────────────────

def _host_of(url: str) -> str:
    if "://" in url:
        url = url.split("://", 1)[1]
    return url.split("/", 1)[0].split(":", 1)[0]


def measure_direct(url: str, timeout: float = 8.0) -> tuple[bool, int | None, str | None]:
    """Замер без прокси — прямой выход в сеть.

    ProxyHandler({}) запрещает urllib брать системный прокси, поэтому
    замер всегда идёт напрямую.
    """
    t0 = time.time()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with opener.open(req, timeout=timeout) as r:
            r.read(1)
        return True, int((time.time() - t0) * 1000), None
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


def _socks5_connect(proxy_host: str, proxy_port: int, target_host: str,
                    target_port: int, timeout: float) -> socket.socket:
    """Открывает TCP-соединение к target через SOCKS5-прокси."""
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    sock.settimeout(timeout)
    # Приветствие: версия 5, одна метода (без авторизации)
    sock.sendall(b"\x05\x01\x00")
    resp = sock.recv(2)
    if len(resp) < 2 or resp[0] != 0x05:
        sock.close()
        raise OSError("не SOCKS5-прокси")
    if resp[1] != 0x00:
        sock.close()
        raise OSError("SOCKS5 требует авторизацию")
    # CONNECT к цели (домен в виде ASCII)
    host_bytes = target_host.encode("idna") if target_host.isascii() else target_host.encode()
    req = b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + \
        target_port.to_bytes(2, "big")
    sock.sendall(req)
    head = sock.recv(4)
    if len(head) < 4 or head[1] != 0x00:
        sock.close()
        raise OSError(f"SOCKS5 CONNECT отклонён (код {head[1] if len(head) > 1 else '?'})")
    atyp = head[3]
    if atyp == 0x01:
        sock.recv(4)
    elif atyp == 0x03:
        ln = sock.recv(1)[0]
        sock.recv(ln)
    elif atyp == 0x04:
        sock.recv(16)
    sock.recv(2)  # порт
    return sock


def measure_via_socks5(url: str, proxy_host: str, proxy_port: int,
                       timeout: float = 8.0) -> tuple[bool, int | None, str | None]:
    """Замер через локальный SOCKS5 — эмулирует путь через туннель."""
    host = _host_of(url)
    t0 = time.time()
    sock = None
    try:
        sock = _socks5_connect(proxy_host, proxy_port, host, 443, timeout)
        # Сертификат не проверяем: замер меряет ЗАДЕРЖКУ маршрута, а не
        # аутентифицирует сервер. С проверкой часть сайтов с нестандартными
        # цепочками сертификатов давала бы ложный «недоступен», хотя маршрут
        # на самом деле работает — и мы выбрали бы неверный маршрут.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            sock = None
            tls.sendall(
                f"GET / HTTP/1.1\r\nHost: {host}\r\nUser-Agent: {UA}\r\n"
                f"Accept: */*\r\nConnection: close\r\n\r\n".encode())
            data = tls.recv(64)
            if not data.startswith(b"HTTP/"):
                raise OSError("не HTTP-ответ")
        return True, int((time.time() - t0) * 1000), None
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def find_local_socks() -> tuple[str, int] | None:
    """Ищет работающий локальный SOCKS5 (для A/B-теста)."""
    for host, port in LOCAL_SOCKS_CANDIDATES:
        try:
            with socket.create_connection((host, port), timeout=1.5):
                return host, port
        except OSError:
            continue
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Накопление статистики
# ─────────────────────────────────────────────────────────────────────────────

class RouteLearner:
    """Хранит историю замеров по доменам и выбирает лучший маршрут."""

    def __init__(self, config: dict):
        self.cfg = config.setdefault("route_learning", {})
        self.cfg.setdefault("enabled", True)
        self.cfg.setdefault("auto_apply", True)
        self.cfg.setdefault("min_samples", 3)
        self.cfg.setdefault("routes", {})
        self.cfg_root = config

    def save(self, config_path: str | None = None) -> bool:
        """Сохраняет конфиг с накопленной статистикой."""
        import json
        if not config_path:
            return False
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(self.cfg_root, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    # ── запись ────────────────────────────────────────────────────────────
    def record(self, domain: str, route: str, ms: int | None, ok: bool,
               error: str | None = None) -> None:
        """Записывает один замер по домену и маршруту."""
        domain = CA._norm(domain)
        if not domain or route not in MEASURED:
            return
        routes = self.cfg.setdefault("routes", {})
        entry = routes.setdefault(domain, {})
        stat = entry.setdefault(route, {"ms": [], "ok": 0, "fail": 0,
                                        "last_error": None, "updated": None})
        if ok and ms is not None:
            stat["ms"].append(int(ms))
            if len(stat["ms"]) > MAX_SAMPLES:
                stat["ms"] = stat["ms"][-MAX_SAMPLES:]
            stat["ok"] += 1
            stat["last_error"] = None
        else:
            stat["fail"] += 1
            stat["last_error"] = error
        stat["updated"] = int(time.time())

    # ── чтение ────────────────────────────────────────────────────────────
    def stats(self, domain: str) -> dict:
        """Сводка по маршрутам домена: среднее, успехи, неудачи."""
        domain = CA._norm(domain)
        entry = self.cfg.get("routes", {}).get(domain, {})
        out = {}
        for route in ASSIGNABLE:
            st = entry.get(route)
            if not st:
                continue
            samples = st.get("ms", []) or []
            avg = int(sum(samples) / len(samples)) if samples else None
            out[route] = {
                "avg_ms": avg,
                "samples": len(samples),
                "ok": st.get("ok", 0),
                "fail": st.get("fail", 0),
                "last_error": st.get("last_error"),
            }
        return out

    def best_route(self, domain: str, min_samples: int | None = None) -> tuple[str | None, int | None]:
        """Возвращает лучший маршрут домена и его среднее время.

        Маршрут считается пригодным, если по нему есть успешные замеры.
        При равенстве средних предпочтение отдаётся более «стабильному»
        маршруту: сначала zapret (обход DPI), потом прямое, потом туннель.
        """
        domain = CA._norm(domain)
        min_samples = int(min_samples if min_samples is not None
                          else self.cfg.get("min_samples", 3))
        stats = self.stats(domain)
        if not stats:
            return None, None
        viable = {r: s for r, s in stats.items()
                  if s["samples"] > 0 and s["avg_ms"] is not None}
        if not viable:
            return None, None
        # При равенстве — порядок предпочтения
        pref = {ROUTE_ZAPRET: 0, ROUTE_DIRECT: 1, ROUTE_VPN: 3}
        best = min(viable.items(),
                   key=lambda kv: (kv[1]["avg_ms"], pref.get(kv[0], 9)))
        route, st = best
        if st["samples"] < min_samples and len(viable) > 1:
            # данных мало — доверяем только если альтернативы заметно хуже
            others = [s["avg_ms"] for r, s in viable.items() if r != route]
            if others and min(others) - st["avg_ms"] < max(150, st["avg_ms"] * 0.3):
                return None, None
        return route, st["avg_ms"]

    def recommendation(self, domain: str) -> dict:
        """Рекомендация по домену: маршрут, среднее, уверенность, причина."""
        domain = CA._norm(domain)
        route, avg = self.best_route(domain)
        stats = self.stats(domain)
        if not route:
            return {"domain": domain, "route": None, "avg_ms": None,
                    "confident": False, "reason": "недостаточно данных"}
        st = stats[route]
        total = st["ok"] + st["fail"]
        reason = f"{ROUTE_LABELS.get(route, route)}: {avg} мс, замеров {st['samples']}"
        if total and st["fail"] > st["ok"]:
            reason += f", неудач {st['fail']}"
        return {"domain": domain, "route": route, "avg_ms": avg,
                "confident": st["samples"] >= int(self.cfg.get("min_samples", 3)),
                "reason": reason}

    def domains(self) -> list[str]:
        return sorted(self.cfg.get("routes", {}).keys())

    def report(self, drm=None) -> list[dict]:
        """Отчёт по всем доменам с рекомендациями."""
        out = []
        for domain in self.domains():
            rec = self.recommendation(domain)
            stats = self.stats(domain)
            rec["stats"] = stats
            cur = self.current_route(domain, drm=drm)
            rec["current"] = cur
            rec["current_label"] = ROUTE_LABELS.get(cur, cur)
            rec["differs"] = bool(rec["route"] and rec["route"] != cur)
            out.append(rec)
        return out

    # ── применение ───────────────────────────────────────────────────────
    def current_route(self, domain: str, drm=None) -> str:
        """Текущий назначенный домену маршрут (по спискам приложения)."""
        domain = CA._norm(domain)
        if not domain:
            return ROUTE_VPN
        drm = drm or getattr(self, "drm", None)
        if drm is None:
            return ROUTE_VPN
        return CA.ConnectionAnalyzer(drm).detect(domain)

    def apply(self, domain: str, route: str | None = None,
              drm=None, analyzer=None) -> tuple[bool, str]:
        """Применяет маршрут к домену (запомненный или указанный).

        Используется автоприменением: меняет назначение домена, но не
        трогает список zapret — zapret это обход DPI, а не путь трафика,
        и удалять его по накопленной статистике нельзя.
        """
        domain = CA._norm(domain)
        if not domain:
            return False, "Пустой домен"
        if route is None:
            route, _ = self.best_route(domain)
            if not route:
                return False, f"Нет запомненного маршрута для {domain}"
        if drm is not None and analyzer is None:
            analyzer = CA.ConnectionAnalyzer(drm)
        if analyzer is None:
            return False, "Нет менеджера маршрутов"

        if route == ROUTE_ZAPRET:
            ok, msg = BP.zapret_add_host(domain)
            if not ok:
                return False, msg
            restarted, rmsg = BP.zapret_restart()
            if not restarted:
                msg = f"{msg}; перезапустите zapret2 ({rmsg})"
            else:
                msg = f"{msg}; {rmsg}"
        else:
            # Снимаем прежнее назначение маршрута, но оставляем zapret
            drm.direct_remove("domains", domain)
            drm.tunnel_remove("domains", domain)
            if route == ROUTE_DIRECT:
                drm.direct_add("domains", domain)
                msg = f"{domain} — прямое подключение (мимо VPN)"
            elif route in (ROUTE_VPN, ROUTE_TUNNEL):
                # Трафик идёт через туннель VPN как обычно
                msg = f"{domain} — через туннель VPN"
            else:
                return False, f"Неизвестный маршрут: {route}"
            drm.save()

        self.cfg.setdefault("applied", {})[domain] = {
            "route": route, "at": int(time.time())}
        return True, msg

    def apply_all(self, drm=None, analyzer=None, only_confident: bool = True) -> list[dict]:
        """Применяет запомненные маршруты ко всем доменам."""
        if drm is not None and analyzer is None:
            analyzer = CA.ConnectionAnalyzer(drm)
        out = []
        for rec in self.report(drm=drm):
            if not rec["route"]:
                continue
            if only_confident and not rec["confident"]:
                continue
            if not rec["differs"]:
                continue
            ok, msg = self.apply(rec["domain"], rec["route"],
                                 drm=drm, analyzer=analyzer)
            out.append({"domain": rec["domain"], "route": rec["route"],
                        "ok": ok, "message": msg})
        return out

    def clear(self, domain: str | None = None) -> None:
        """Сбрасывает накопленную статистику."""
        if domain:
            self.cfg.get("routes", {}).pop(CA._norm(domain), None)
        else:
            self.cfg["routes"] = {}


def detect_current_route(domain: str, drm, connected: bool) -> str:
    """Определяет маршрут, по которому фактически пойдёт трафик домена.

    Учитывает состояние подключения и списки приложения:
    - домен в `direct_routes` → прямое соединение;
    - домен в `tunnel_routes` → туннель;
    - домен в списке zapret → zapret;
    - подключено → обычный VPN, отключено → прямое соединение.
    """
    domain = CA._norm(domain)
    if drm.is_direct_enabled() and CA._matches(domain, drm.cfg.get("domains", [])):
        return ROUTE_DIRECT
    if BP.zapret_covers(domain):
        return ROUTE_ZAPRET
    # Принудительный туннель и обычный VPN — физически один путь,
    # поэтому измеряем их одинаково.
    if connected or (drm.is_tunnel_enabled()
                     and CA._matches(domain, drm.tcfg.get("domains", []))):
        return ROUTE_VPN
    return ROUTE_DIRECT
