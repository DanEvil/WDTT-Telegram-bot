"""Простые метрики в формате Prometheus (без внешних зависимостей).

Эндпоинт /metrics отдаёт текст в формате exposition format:
    # HELP wdtt_bot_payments_total Общее число платежей
    # TYPE wdtt_bot_payments_total counter
    wdtt_bot_payments_total{status="succeeded"} 42
"""
import threading
import time
from collections import defaultdict

_lock = threading.Lock()
_counters: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
_gauges: dict[str, float] = {}
_histograms: dict[str, list[float]] = defaultdict(list)


def inc_counter(name: str, value: float = 1.0, **labels) -> None:
    """Увеличить счётчик. labels — пары ключ=значение для размерностей."""
    key = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    with _lock:
        _counters[name][key] += value


def set_gauge(name: str, value: float) -> None:
    with _lock:
        _gauges[name] = value


def observe(name: str, value: float) -> None:
    """Записать значение в гистограмму (для латентности)."""
    with _lock:
        _histograms[name].append(value)
        if len(_histograms[name]) > 1000:
            _histograms[name] = _histograms[name][-1000:]


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render() -> str:
    """Сформировать текст метрик в формате Prometheus."""
    lines = []
    with _lock:
        for name, values in sorted(_counters.items()):
            lines.append(f"# TYPE {name} counter")
            for key, val in sorted(values.items()):
                label = f"{{{key}}}" if key else ""
                lines.append(f"{name}{label} {val:g}")
        for name, val in sorted(_gauges.items()):
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {val:g}")
        for name, vals in sorted(_histograms.items()):
            if not vals:
                continue
            lines.append(f"# TYPE {name} summary")
            s = sorted(vals)
            lines.append(f"{name}_count {len(s):g}")
            lines.append(f"{name}_sum {sum(s):g}")
            for q in (0.5, 0.9, 0.99):
                idx = min(int(q * len(s)), len(s) - 1)
                lines.append(f'{name}{{quantile="{q}"}} {s[idx]:g}')
    return "\n".join(lines) + "\n"


# --- предопределённые метрики бота ---

PAYMENTS_TOTAL = "wdtt_bot_payments_total"
PAYMENTS_AMOUNT_RUB = "wdtt_bot_payments_amount_rub"
USERS_ACTIVE = "wdtt_bot_users_active"
USERS_TOTAL = "wdtt_bot_users_total"
PANEL_UP = "wdtt_panel_up"
PANEL_LATENCY_SECONDS = "wdtt_panel_latency_seconds"
WEBHOOK_RECEIVED = "wdtt_webhook_received_total"
MESSAGES_TOTAL = "wdtt_bot_messages_total"
ERRORS_TOTAL = "wdtt_bot_errors_total"
