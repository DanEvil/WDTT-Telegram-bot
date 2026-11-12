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
# (иногда удобно ставить один общий хэш); если пусто, бот сам создаст вызовы.
WDTT_VK_HASH = os.getenv("WDTT_VK_HASH", "").strip()

# Мульти-сервер: лимит пользователей на одну панель WDTT (inbound max_users)
SERVER_MAX_USERS = _as_int("SERVER_MAX_USERS", 100)
# Сколько VK-хешей выдавать каждому пользователю (панель: не более 4 на профиль)
VK_HASHES_PER_USER = _as_int("VK_HASHES_PER_USER", 4)
# Автоустановка панели по SSH: таймауты (сек)
SSH_CONNECT_TIMEOUT = _as_int("SSH_CONNECT_TIMEOUT", 30)
PANEL_INSTALL_TIMEOUT = _as_int("PANEL_INSTALL_TIMEOUT", 900)
# URL установщика панели (ildarmaga/wdtt deploy.sh)
PANEL_INSTALLER_URL = os.getenv(
    "PANEL_INSTALLER_URL",
    "https://raw.githubusercontent.com/ildarmaga/wdtt/main/deploy.sh",
).strip()

# Брендинг сайта / мини-аппа
BRAND_NAME = os.getenv("BRAND_NAME", "K.O. Gateway").strip()
# Username бота в Telegram (без @) — для ссылок на бота с сайта: https://t.me/<username>
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "").strip()

# Ссылки на клиентские приложения (как в боте)
APP_ANDROID_URL = os.getenv(
    "APP_ANDROID_URL",
    "https://github.com/amurcanov/proxy-turn-vk-android/releases/download/v1.2.4/WDTT-universal.apk",
).strip()
APP_IOS_TESTFLIGHT_URL = os.getenv("APP_IOS_TESTFLIGHT_URL", "https://apps.apple.com/app/testflight/id899247664").strip()
APP_IOS_JOIN_URL = os.getenv("APP_IOS_JOIN_URL", "https://testflight.apple.com/join/ANm6cmDv").strip()
APP_WINDOWS_URL = os.getenv(
    "APP_WINDOWS_URL",
    "https://github.com/ildarmaga/wdtt/releases/download/v1.5.76/pwdtt-client-windows-amd64.exe",
).strip()

# OpenFlux: дефолтные параметры подключения (URL документа-транспорта и секрет шифрования).
# Переопределяются из админ-панели (настройки openflux_url / openflux_secret).
OPENFLUX_URL = os.getenv("OPENFLUX_URL", "").strip()
OPENFLUX_SECRET = os.getenv("OPENFLUX_SECRET", "").strip()

# Бот
DB_PATH = os.getenv("DB_PATH", "bot.db").strip()
POLL_INTERVAL = _as_int("POLL_INTERVAL", 10)

# Логирование
LOG_FILE = os.getenv("LOG_FILE", "").strip()          # пусто = только stdout
LOG_MAX_BYTES = _as_int("LOG_MAX_BYTES", 10 * 1024 * 1024)  # 10 МБ
LOG_BACKUP_COUNT = _as_int("LOG_BACKUP_COUNT", 5)

# Мониторинг панели: алерт админу, если панель недоступна дольше N проверок подряд
PANEL_FAIL_ALERT_THRESHOLD = _as_int("PANEL_FAIL_ALERT_THRESHOLD", 3)
PANEL_CHECK_INTERVAL = _as_int("PANEL_CHECK_INTERVAL", 60)  # сек

# Метрики Prometheus
METRICS_ENABLED = _as_bool("METRICS_ENABLED", True)
# Если задан — /metrics требует заголовок "Authorization: Bearer <токен>".
# Без токена метрики отдаются только с loopback (127.0.0.1/::1).
METRICS_TOKEN = os.getenv("METRICS_TOKEN", "").strip()

# Сайт K.O. Gateway (порт WEB_PORT) — отдельный HTTP-сервер, запускается ботом
WEB_ENABLED = _as_bool("WEB_ENABLED", True)
WEB_PORT = _as_int("WEB_PORT", 8080)
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0").strip()
# Telegram Mini App (WebApp)
WEBAPP_ENABLED = _as_bool("WEBAPP_ENABLED", True)
WEBAPP_HOST = os.getenv("WEBAPP_HOST", "0.0.0.0").strip()
WEBAPP_PORT = _as_int("WEBAPP_PORT", 8082)
# Публичный URL сайта (https://...) — ссылка на сайт из мини-аппа
SITE_URL = os.getenv("SITE_URL", "").strip().rstrip("/")
# Публичный URL приложения (https://...). Через него же бот отдаёт кнопку WebApp.
MINIAPP_URL = os.getenv("MINIAPP_URL", "").strip().rstrip("/")
# Оплата Telegram Stars
STARS_ENABLED = _as_bool("STARS_ENABLED", True)
STARS_RATE = float(os.getenv("STARS_RATE", "1.0"))  # сколько звёзд за 1 рубль

# RedisStorage для FSM (multi-instance). Если REDIS_URL доступен — используем,
# иначе MemoryStorage (single-instance).
USE_REDIS_STORAGE = _as_bool("USE_REDIS_STORAGE", False)

# Webhook ЮKassa
WEBHOOK_ENABLED = _as_bool("WEBHOOK_ENABLED", False)
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0").strip()
# ВАЖНО: не должен совпадать с WEB_PORT (сайт), WEBAPP_PORT и HEALTH_PORT —
# все серверы поднимаются в одном процессе на одном хосте.
WEBHOOK_PORT = _as_int("WEBHOOK_PORT", 8090)
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/yookassa").strip()
# Отключить IP-whitelist ЮKassa (только для локальной разработки за прокси!)
WEBHOOK_SKIP_IP_CHECK = _as_bool("WEBHOOK_SKIP_IP_CHECK", False)
# Healthcheck endpoint (для Docker HEALTHCHECK)
HEALTH_PORT = _as_int("HEALTH_PORT", 8081)

# Реферальная программа
REFERRAL_REWARD_DAYS = _as_int("REFERRAL_REWARD_DAYS", 3)
REFERRAL_REWARD_TRAFFIC_GB = _as_int("REFERRAL_REWARD_TRAFFIC_GB", 0)
REFERRAL_REWARD_MAX_DEVICES = _as_int("REFERRAL_REWARD_MAX_DEVICES", 1)

# Единый домен подписки (Subscription Proxy)
# Все пользователи получают ссылку вида https://SUB_DOMAIN/sub/{sub_id}
# Nginx с Lua маршрутизирует запрос к нужной панели по маппингу в Redis
SUB_DOMAIN = os.getenv("SUB_DOMAIN", "").strip()  # например: https://sub.yourdomain.com
SUB_PATH = os.getenv("SUB_PATH", "/sub/").strip()  # путь для подписки

# Redis для маппинга sub_id -> panel_base_url
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0").strip()
REDIS_SUB_TTL = _as_int("REDIS_SUB_TTL", 86400 * 30)  # TTL записи в секундах (30 дней)


def _duplicate_port_errors(ports: dict[str, int]) -> list[str]:
    """Ошибки о совпадающих портах (все HTTP-серверы живут в одном процессе)."""
    errors: list[str] = []
    seen: dict[int, str] = {}
    for name, port in ports.items():
        if port in seen:
            errors.append(f"{name} и {seen[port]} совпадают ({port}) — задайте разные порты")
        else:
            seen[port] = name
    return errors


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

    # Все HTTP-серверы поднимаются в одном процессе на одном хосте:
    # совпадение портов = молчаливое падение одной из задач.
    ports: dict[str, int] = {"HEALTH_PORT": HEALTH_PORT}
    if WEB_ENABLED:
        ports["WEB_PORT"] = WEB_PORT
    if WEBAPP_ENABLED:
        ports["WEBAPP_PORT"] = WEBAPP_PORT
    if WEBHOOK_ENABLED:
        ports["WEBHOOK_PORT"] = WEBHOOK_PORT
    errors.extend(_duplicate_port_errors(ports))
    return errors
