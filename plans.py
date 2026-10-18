"""Тарифы подписки. Можно переопределить через переменную окружения PLANS_JSON."""
import json
import os

# Тарифы по умолчанию (пример — поменяйте под свою ценовую политику).
# Цена = 100 ₽/мес × продолжительность в месяцах; трафик безлимитный у всех.
DEFAULT_PLANS = [
    {
        "id": "month",
        "name": "Месяц",
        "days": 30,
        "price": 100,
        "traffic_gb": 0,
        "max_devices": 3,
        "description": "30 дней, безлимит трафика, 3 устройства",
    },
    {
        "id": "quarter",
        "name": "3 месяца",
        "days": 90,
        "price": 300,
        "traffic_gb": 0,
        "max_devices": 3,
        "description": "90 дней, безлимит трафика, 3 устройства",
    },
    {
        "id": "half_year",
        "name": "6 месяцев",
        "days": 180,
        "price": 600,
        "traffic_gb": 0,
        "max_devices": 3,
        "description": "180 дней, безлимит трафика, 3 устройства",
    },
    {
        "id": "year",
        "name": "12 месяцев",
        "days": 365,
        "price": 1200,
        "traffic_gb": 0,
        "max_devices": 3,
        "description": "365 дней, безлимит трафика, 3 устройства",
    },
]


def _load_plans() -> list[dict]:
    raw = os.getenv("PLANS_JSON", "").strip()
    if not raw:
        return DEFAULT_PLANS
    plans = json.loads(raw)
    if not isinstance(plans, list):
        raise ValueError("PLANS_JSON должен быть JSON-массивом тарифов")
    return plans


PLANS: list[dict] = _load_plans()
PLANS_BY_ID: dict[str, dict] = {p["id"]: p for p in PLANS}

# Бесплатный пробный период — не входит в PLANS и не проходит через оплату.
_trial_days = int(os.getenv("TRIAL_DAYS", "3"))
_trial_gb = int(os.getenv("TRIAL_TRAFFIC_GB", "5"))
_trial_devices = int(os.getenv("TRIAL_MAX_DEVICES", "1"))

TRIAL: dict = {
    "id": "trial",
    "name": "Пробный период",
    "days": _trial_days,
    "price": 0,
    "traffic_gb": _trial_gb,
    "max_devices": _trial_devices,
    "description": f"{_trial_days} дн., {_trial_gb} ГБ, {_trial_devices} устр. — бесплатно",
}

# Единый справочник: платные тарифы + пробный период (для выдачи доступа).
ALL_PLANS_BY_ID: dict[str, dict] = {**PLANS_BY_ID, TRIAL["id"]: TRIAL}
