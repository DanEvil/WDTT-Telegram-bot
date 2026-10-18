"""Конфигурация бота. Все значения берутся из переменных окружения / файла .env."""
import os

from dotenv import load_dotenv

load_dotenv()


def _as_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _parse_admin_ids(raw: str) -> set[int]:
    result = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            result.add(int(part))
    return result


# Telegram
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_IDS = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))

# ЮKassa
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
RETURN_URL = os.getenv("RETURN_URL", "https://t.me/").strip()

# WDTT панель
WDTT_BASE_URL = os.getenv("WDTT_BASE_URL", "https://127.0.0.1:2860/wdtt").strip()
WDTT_USERNAME = os.getenv("WDTT_USERNAME", "admin").strip()
WDTT_PASSWORD = os.getenv("WDTT_PASSWORD", "wdtt").strip()
WDTT_VERIFY_SSL = _as_bool("WDTT_VERIFY_SSL", False)
# vk_hash, который прописывается новым пользователям вместо создания VK-вызова
# (иногда удобно ставить один общий хэш); если пусто, бот сам создаст вызов.
WDTT_VK_HASH = os.getenv("WDTT_VK_HASH", "").strip()

# Бот
DB_PATH = os.getenv("DB_PATH", "bot.db").strip()
POLL_INTERVAL = _as_int("POLL_INTERVAL", 10)

# Webhook ЮKassa
WEBHOOK_ENABLED = _as_bool("WEBHOOK_ENABLED", False)
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0").strip()
WEBHOOK_PORT = _as_int("WEBHOOK_PORT", 8080)
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/yookassa").strip()

# Реферальная программа
REFERRAL_REWARD_DAYS = _as_int("REFERRAL_REWARD_DAYS", 3)
REFERRAL_REWARD_TRAFFIC_GB = _as_int("REFERRAL_REWARD_TRAFFIC_GB", 0)
REFERRAL_REWARD_MAX_DEVICES = _as_int("REFERRAL_REWARD_MAX_DEVICES", 1)


def validate() -> list[str]:
    """Возвращает список ошибок конфигурации (пустой, если всё в порядке)."""
    errors = []
    if not TELEGRAM_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN не задан")
    if not YOOKASSA_SHOP_ID:
        errors.append("YOOKASSA_SHOP_ID не задан")
    if not YOOKASSA_SECRET_KEY:
        errors.append("YOOKASSA_SECRET_KEY не задан")
    if not ADMIN_IDS:
        errors.append("ADMIN_IDS пуст — укажите ваш Telegram ID")
    return errors
