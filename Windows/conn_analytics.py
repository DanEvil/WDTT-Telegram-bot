"""Аналитика типа соединения: какой трафик идёт как.

Определяет для домена, каким способом он сейчас обрабатывается:

- `direct`  — прямое подключение, мимо VPN (direct_routes);
- `tunnel`  — принудительно через туннель VPN (tunnel_routes);
- `zapret`  — через zapret2: обход DPI-фильтрации по списку myhostlist.txt;
- `vpn`     — обычный трафик через активный VPN-профиль;
- `blocked` — доступ невозможен (блокировка или замедление), zapret не помогает.

Приоритет: исключение из туннеля важнее обхода DPI, а zapret — важнее
обычного VPN-маршрута, потому что он снимает именно фильтрацию оператора.
"""

from __future__ import annotations

import os

try:
    import bypass as BP
except ImportError:  # pragma: no cover — запуск вне каталога Windows
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import bypass as BP

# Подписи и порядок вывода в UI
TYPE_LABELS = {
    "direct": "Прямое",
    "tunnel": "Туннель",
    "zapret": "Zapret",
    "vpn": "VPN",
    "blocked": "Заблокирован",
}

TYPE_ORDER = ["zapret", "direct", "tunnel", "vpn", "blocked"]


def _norm(domain: str) -> str:
    d = (domain or "").strip().lower()
    if "://" in d:
        d = d.split("://", 1)[1]
    return d.split("/", 1)[0].split(":", 1)[0]


def _matches(domain: str, patterns: list[str]) -> bool:
    """Домен совпадает с шаблоном списка (точный, поддомен или *.шаблон).

    Семантика совпадает с bypass.domain_in_list — иначе zapret и маршруты
    вели бы себя по-разному на одних и тех же доменах.
    """
    return BP.domain_in_list(domain, patterns or [])


class ConnectionAnalyzer:
    """Определяет тип соединения для доменов и собирает сводку аналитики."""

    def __init__(self, drm):
        self.drm = drm

    # ── списки маршрутов ──────────────────────────────────────────────────
    def direct_domains(self) -> list[str]:
        return list(self.drm.cfg.get("domains", []) or [])

    def tunnel_domains(self) -> list[str]:
        return list(self.drm.tcfg.get("domains", []) or [])

    # ── определение типа ─────────────────────────────────────────────────
    def detect(self, domain: str, blocked: bool = False) -> str:
        """Возвращает тип соединения для домена."""
        domain = _norm(domain)
        if not domain:
            return "vpn"

        if self.drm.is_direct_enabled() and _matches(domain, self.direct_domains()):
            return "direct"
        if self.drm.is_tunnel_enabled() and _matches(domain, self.tunnel_domains()):
            return "tunnel"
        if BP.zapret_covers(domain):
            return "zapret"
        if blocked:
            return "blocked"
        return "vpn"

    def label(self, domain: str, blocked: bool = False) -> str:
        """Подпись типа соединения для UI."""
        return TYPE_LABELS.get(self.detect(domain, blocked), TYPE_LABELS["vpn"])

    # ── сводка ───────────────────────────────────────────────────────────
    def summary(self, domains: list[str], blocked: list[str] | None = None) -> dict:
        """Сводка по типам соединений для набора доменов.

        domains  — домены для анализа;
        blocked  — домены, до которых нет доступа (помечаются как blocked).
        """
        blocked_set = {_norm(d) for d in (blocked or [])}
        counts = {t: 0 for t in TYPE_ORDER}
        rows = []
        for domain in domains or []:
            d = _norm(domain)
            kind = self.detect(d, blocked=d in blocked_set)
            counts[kind] = counts.get(kind, 0) + 1
            rows.append({"domain": d, "type": kind, "label": TYPE_LABELS.get(kind, kind)})
        return {
            "rows": rows,
            "counts": counts,
            "zapret": BP.zapret_state(),
        }

    def all_known_domains(self) -> list[str]:
        """Все домены, известные приложению: маршруты, zapret, проверяемые сайты."""
        out = []
        for d in self.direct_domains():
            out.append(d)
        for d in self.tunnel_domains():
            out.append(d)
        out.extend(BP.zapret_hosts())
        return sorted({_norm(d) for d in out if _norm(d)})

    # ── применение типа ──────────────────────────────────────────────────
    def set_type(self, domain: str, kind: str) -> tuple[bool, str]:
        """Назначает домену тип соединения.

        kind: direct | tunnel | zapret | vpn (снять все назначения).
        """
        domain = _norm(domain)
        if not domain:
            return False, "Пустой домен"

        # Снимаем прежние назначения, чтобы тип был однозначным
        self.drm.direct_remove("domains", domain)
        self.drm.tunnel_remove("domains", domain)

        if kind == "direct":
            self.drm.direct_add("domains", domain)
            self.drm.save()
            return True, f"{domain} — прямое подключение (мимо VPN)"
        if kind == "tunnel":
            self.drm.tunnel_add("domains", domain)
            self.drm.save()
            return True, f"{domain} — через туннель VPN"
        if kind == "zapret":
            ok, msg = BP.zapret_add_host(domain)
            if not ok:
                return False, msg
            restarted, rmsg = BP.zapret_restart()
            if restarted:
                return True, f"{domain} — zapret; {rmsg}"
            return True, f"{domain} — zapret; перезапустите zapret2 ({rmsg})"
        if kind == "vpn":
            BP.zapret_remove_host(domain)
            self.drm.save()
            return True, f"{domain} — обычный трафик через VPN"

        return False, f"Неизвестный тип соединения: {kind}"
