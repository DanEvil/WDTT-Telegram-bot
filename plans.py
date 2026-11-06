"""Тарифы подписки. Можно переопределить через переменную окружения PLANS_JSON."""
import json
import os

# Тарифы по умолчанию (переопределяются через PLANS_JSON).
# Все платные тарифы — безлимит трафика; лимит трафика есть только у пробного периода.
# Названия — короткие (Month, Year...), детали (дни/лимиты/устройства) — в description.
DEFAULT_PLANS = [
    {
        "id": "month",
        "name": "Month",
        "days": 30,
        "price": 100,
        "traffic_gb": 0,
        "max_devices": 3,
        "description": "30 дней · безлимит трафика · до 3 устройств",
    },
    {
        "id": "quarter",
        "name": "Quarter",
        "days": 90,
        "price": 300,
        "traffic_gb": 0,
        "max_devices": 3,
        "description": "90 дней · безлимит трафика · до 3 устройств",
    },
    {
        "id": "half_year",
        "name": "Half-Year",
        "days": 180,
        "price": 600,
        "traffic_gb": 0,
        "max_devices": 3,
        "description": "180 дней · безлимит трафика · до 3 устройств",
    },
    {
        "id": "year",
        "name": "Year",
        "days": 365,
        "price": 1200,
        "traffic_gb": 0,
        "max_devices": 3,
        "description": "365 дней · безлимит трафика · до 3 устройств",
    },
]

# Аддоны «больше устройств»: days=0 — не продлевают подписку, меняют только max_devices.
# Цена фиксированная: до 5 устройств +100 ₽, до 10 устройств +200 ₽ к любому тарифу.
DEVICE_ADDONS = [
    {
        "id": "devices5",
        "name": "До 5 устройств",
        "days": 0,
        "price": 100,
        "traffic_gb": 0,
        "max_devices": 5,
        "addon": True,
        "description": "+2 устройства к подписке (итого до 5) — +100 ₽",
    },
    {
        "id": "devices10",
        "name": "До 10 устройств",
        "days": 0,
        "price": 200,
        "traffic_gb": 0,
        "max_devices": 10,
        "addon": True,
        "description": "+7 устройств к подписке (итого до 10) — +200 ₽",
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
DEVICE_ADDONS_BY_ID: dict[str, dict] = {p["id"]: p for p in DEVICE_ADDONS}

# Бесплатный пробный период — не входит в PLANS и не проходит через оплату.
# Единственный лимитированный по трафику тариф (по умолчанию 5 ГБ).
_trial_days = int(os.getenv("TRIAL_DAYS", "3"))
_trial_gb = int(os.getenv("TRIAL_TRAFFIC_GB", "5"))
_trial_devices = int(os.getenv("TRIAL_MAX_DEVICES", "1"))

TRIAL: dict = {
    "id": "trial",
    "name": "Trial",
    "days": _trial_days,
    "price": 0,
    "traffic_gb": _trial_gb,
    "max_devices": _trial_devices,
    "description": f"{_trial_days} дн. · {_trial_gb} ГБ · {_trial_devices} устр. — бесплатно",
}

# Единый справочник: платные тарифы + аддоны устройств + пробный период (для выдачи доступа).
ALL_PLANS_BY_ID: dict[str, dict] = {**PLANS_BY_ID, **DEVICE_ADDONS_BY_ID, TRIAL["id"]: TRIAL}
