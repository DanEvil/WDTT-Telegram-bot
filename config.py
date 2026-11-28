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
# Только серверные (env); НЕ отдаются пользователям и больше не редактируются из админ-панели.
OPENFLUX_URL = os.getenv("OPENFLUX_URL", "").strip()
OPENFLUX_SECRET = os.getenv("OPENFLUX_SECRET", "").strip()
# Базовая openflux://v1/ ссылка exit-ноды (печатается --share в journal openflux.service).
# Из неё link_converter.personalize_link делает персональную ссылку каждому клиенту
# (свой name, свой doc URL если задан в настройках клиента); secret остаётся общим.
OPENFLUX_BASE_LINK = os.getenv("OPENFLUX_BASE_LINK", "").strip()

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
WEB_COOKIE_SECURE = _as_bool("WEB_COOKIE_SECURE", True)
# Публичный URL приложения (https://...). Через него же бот отдаёт кнопку WebApp.
MINIAPP_URL = os.getenv("MINIAPP_URL", "").strip().rstrip("/")
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

# VLESS Configuration (Reality по умолчанию)
VLESS_ENABLED = _as_bool("VLESS_ENABLED", True)
VLESS_SERVER = os.getenv("VLESS_SERVER", "").strip()  # VLESS server address (domain or IP)
VLESS_PORT = _as_int("VLESS_PORT", 443)
VLESS_UUID = os.getenv("VLESS_UUID", "").strip()  # UUID for VLESS users (can be same or per-user)
VLESS_PROTOCOL = os.getenv("VLESS_PROTOCOL", "tcp").strip()  # tcp, xhttp, ws, grpc
VLESS_TLS = _as_bool("VLESS_TLS", True)
# Режим безопасности: reality (маскировка под чужой TLS), tls (свой сертификат) или none
VLESS_SECURITY = os.getenv("VLESS_SECURITY", "reality").strip()
VLESS_PATH = os.getenv("VLESS_PATH", "").strip()  # path для xhttp/ws
VLESS_HOST = os.getenv("VLESS_HOST", "").strip()  # Host header для xhttp/ws
VLESS_FINGERPRINT = os.getenv("VLESS_FINGERPRINT", "chrome").strip()
VLESS_ALPN = os.getenv("VLESS_ALPN", "h2,http/1.1").strip()
VLESS_REMARK = os.getenv("VLESS_REMARK", "K.O. Gateway").strip()
# Reality: public key (pbk), short id (sid), SNI и flow — обязательны для рабочей ссылки
VLESS_PUBLIC_KEY = os.getenv("VLESS_PUBLIC_KEY", "").strip()
VLESS_SHORT_ID = os.getenv("VLESS_SHORT_ID", "").strip()
# SNI для VLESS+REALITY. В режиме «белых списков» РФ пропускают только
# whitelisted-домены (ya.ru, yandex.ru, rutube.ru, gosuslugi.ru, sun6-21.userapi.com …),
# поэтому дефолт — ya.ru (см. docs/white-sni-ru.txt). Должен совпадать с
# realitySettings.serverNames в /etc/xray-vless/config.json на сервере.
VLESS_SNI = os.getenv("VLESS_SNI", "ya.ru").strip()
VLESS_FLOW = os.getenv("VLESS_FLOW", "xtls-rprx-vision").strip()

# Управление пользователями VLESS-сервера (per-user UUID через xray API по SSH).
# Выключено по умолчанию: без этих переменных бот работает с общей ссылкой.
# При VLESS_PER_USER=true каждому клиенту выдаётся свой UUID, который добавляется
# на сервер через `xray api adu` и удаляется через `xray api rmu` (без рестарта xray).
VLESS_PER_USER = _as_bool("VLESS_PER_USER", False)
VLESS_SSH_HOST = os.getenv("VLESS_SSH_HOST", "").strip()
VLESS_SSH_PORT = _as_int("VLESS_SSH_PORT", 22)
VLESS_SSH_USER = os.getenv("VLESS_SSH_USER", "root").strip()
VLESS_SSH_PASSWORD = os.getenv("VLESS_SSH_PASSWORD", "").strip()
VLESS_XRAY_BIN = os.getenv("VLESS_XRAY_BIN", "/usr/local/xray-vless/bin/xray").strip()
VLESS_API_SERVER = os.getenv("VLESS_API_SERVER", "127.0.0.1:62790").strip()
VLESS_INBOUND_TAG = os.getenv("VLESS_INBOUND_TAG", "vless-in").strip()
VLESS_INBOUND_PORT = _as_int("VLESS_INBOUND_PORT", 443)
# Файл со списком клиентов на VLESS-сервере: пользователи, добавленные через
# xray API, живут только в памяти и теряются при рестарте — этот файл
# перезаливается в xray сервисом xray-vless-sync.service.
VLESS_CLIENTS_FILE = os.getenv("VLESS_CLIENTS_FILE", "/etc/xray-vless/users.json").strip()

# --- VLESS XHTTP (режим «белых списков» РФ) ---
# Второй инбаунд xray (vless-xhttp-in) прячется за фронтенд-доменом
# (traefik/nginx на 2.56.120.156 → 78.17.16.23:20500), поэтому трафик выглядит
# как обычный HTTPS к whitelisted-домену. Клиентам отдаётся отдельная ссылка
# vless://…?type=xhttp&security=tls&sni=<фронтенд-домен>&path=/xhttp…
VLESS_XHTTP_ENABLED = _as_bool("VLESS_XHTTP_ENABLED", False)
VLESS_XHTTP_HOST = os.getenv("VLESS_XHTTP_HOST", "").strip()  # фронтенд-домен, напр. app.ko-gateway.ru (SNI и Host)
VLESS_XHTTP_PORT = _as_int("VLESS_XHTTP_PORT", 443)          # порт фронтенда
VLESS_XHTTP_PATH = os.getenv("VLESS_XHTTP_PATH", "/xhttp").strip()
VLESS_XHTTP_MODE = os.getenv("VLESS_XHTTP_MODE", "packet-up").strip()
VLESS_XHTTP_UPLINK_PLACEMENT = os.getenv("VLESS_XHTTP_UPLINK_PLACEMENT", "header").strip()
VLESS_XHTTP_CHUNK = _as_int("VLESS_XHTTP_CHUNK", 16384)          # uplinkChunkSize
VLESS_XHTTP_SC_MAX = _as_int("VLESS_XHTTP_SC_MAX", 32768)        # scMaxEachPostBytes
VLESS_XHTTP_SC_MIN_MS = _as_int("VLESS_XHTTP_SC_MIN_MS", 10)     # scMinPostsIntervalMs
VLESS_XHTTP_INBOUND_TAG = os.getenv("VLESS_XHTTP_INBOUND_TAG", "vless-xhttp-in").strip()
VLESS_XHTTP_INBOUND_PORT = _as_int("VLESS_XHTTP_INBOUND_PORT", 20500)
VLESS_XHTTP_REMARK = os.getenv("VLESS_XHTTP_REMARK", "K.O. Gateway").strip()
# Пары path:remark для VLESS-подписки (в Happ/v2ray раскрываются как отдельные серверы)
VLESS_XHTTP_SUB_PATHS = os.getenv("VLESS_XHTTP_SUB_PATHS", "/xhttp:K.O. Gateway,/xhttp-of:K.O. Gateway (OpenFlux)").strip()
# Одна общая VLESS-подписка (раскрывается в серверы в Happ/v2rayNG).
# Если задана — в профиле пользователя вместо отдельных vless-ссылок отдаём только её.
# Пример: https://<id>.containers.yandexcloud.net/vless-sub
# Плейсхолдер {uuid} подставляет персональный UUID клиента (иначе — общий VLESS_UUID).
VLESS_SUB_URL = os.getenv("VLESS_SUB_URL", "").strip()


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
