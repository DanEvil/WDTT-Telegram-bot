"""Telegram-бот продажи подписок на WDTT с оплатой через ЮKassa.

Клиент платит -> бот создаёт/продлевает пользователя в панели WDTT
(https://github.com/ildarmaga/wdtt) и выдаёт ссылку wdtt://.
"""

import asyncio
import functools
import html
import json
import logging
import re
import secrets
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime

import redis.asyncio as redis

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

import config
from db import (
    add_payment,
    add_referral,
    add_server,
    add_vk_account,
    backfill_customer_servers,
    claim_referral_reward,
    clear_customer,
    delete_plan,
    delete_server,
    delete_vk_account,
    get_active_customers,
    get_all_customers,
    get_bind_code,
    get_customer,
    get_customer_by_password,
    get_customers_without_sub_id,
    get_expired_customers,
    get_payment,
    get_pending_payments,
    get_plan,
    get_referrals_count,
    get_server,
    get_site_user_by_id,
    get_site_user_by_referral_code,
    get_vk_account,
    has_used_trial,
    init_db,
    conn as db_conn,
    claim_reminder,
    claim_traffic_notice,
    list_plans,
    list_servers,
    list_vk_accounts,
    mark_bind_code_used,
    confirm_login_request,
    create_auto_login,
    record_vk_issued,
    claim_trial,
    mark_referral_rewarded,
    release_trial,
    server_active_counts,
    bind_site_telegram,
    set_customer_sub_id,
    set_payment_status,
    stats,
    upsert_customer,
    upsert_plan,
    vk_hashes_for_user,
    vk_issued_counts,
    set_payment_method,
    list_autorenew_customers,
    mark_renew_notified,
    clear_renew_notified,
    get_renew_charge,
    mark_renew_charge,
    clear_renew_charge,
)
from payments import create_payment, fetch_payment, charge_saved_method
from plans import ALL_PLANS_BY_ID, DEVICE_ADDONS, PLANS, TRIAL
from wdtt import WdttClient, WdttError
from server_setup import SetupError, deploy_server
from server_select import pick_least_loaded_server_id

import logging.handlers

_log_handlers: list[logging.Handler] = [logging.StreamHandler()]
if config.LOG_FILE:
    _log_handlers.append(logging.handlers.RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding="utf-8",
    ))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=_log_handlers,
)
log = logging.getLogger("bot")
# отдельный логгер для middleware (внутри функций имя log перекрывается)
_bot_log = logging.getLogger("bot")

bot: Bot | None = None
bot_username: str | None = None
fulfill_lock = asyncio.Lock()
clients: dict[int, WdttClient] = {}
redis_client: redis.Redis | None = None

router = Router()

# ---- логирование входящих сообщений (подчистка чата отключена) ----

ADMIN_NAV_BUTTONS = {
    "📊 Статистика", "👥 Клиенты", "⛔ Отозвать", "➕ Продлить", "🎁 Подарить",
    "🖥 Серверы", "➕ Добавить сервер", "🚀 Установить сервер", "🗑 Удалить сервер",
    "🔗 Ссылки серверов",
    "🔑 VK аккаунты", "➕ VK аккаунт", "🗑 VK аккаунт", "🔄 VK хеши добить",
    "🗂 Тарифы", "➕ Добавить тариф", "✏️ Изменить тариф", "🗑 Удалить тариф",
    "⚙️ Админка",
}


@router.message.outer_middleware()
async def log_incoming(handler, event, data):
    """Логирует входящие сообщения. Переписку в чате больше не удаляем."""
    text = event.text or ""
    if text:
        _bot_log.info("входящее от %s: %r", event.from_user.id, text[:40])
    return await handler(event, data)


class AdminStates(StatesGroup):
    revoke_id = State()
    extend_data = State()
    gift_id = State()
    gift_days = State()
    add_server_name = State()
    add_server_url = State()
    add_server_login = State()
    add_server_password = State()
    del_server_id = State()
    # автоустановка панели по SSH
    install_name = State()
    install_host = State()
    install_ssh_port = State()
    install_ssh_user = State()
    install_ssh_password = State()
    # VK-аккаунты
    vk_add_label = State()
    vk_add_cookies = State()
    vk_del_id = State()
    # ссылки пользователей конкретного сервера
    server_links_id = State()
    plan_add_id = State()
    plan_add_name = State()
    plan_add_days = State()
    plan_add_price = State()
    plan_add_traffic = State()
    plan_add_devices = State()
    plan_edit_id = State()
    plan_edit_values = State()
    plan_del_id = State()


# текст кнопки -> id тарифа (заполняется при показе выбора тарифа)
PLAN_BUTTONS: dict[str, str] = {}

NAV_BUTTONS = {
    "🎁 Пробный период", "🛒 Купить", "👤 Моя подписка", "👥 Рефералы",
    "📖 Инструкция", "🤖 Android", "🍎 iOS",
    "❓ Помощь", "🏠 Главное меню", "📊 Статистика",
    "👥 Клиенты", "⛔ Отозвать", "➕ Продлить", "🎁 Подарить", "🖥 Серверы",
    "➕ Добавить сервер", "🚀 Установить сервер", "🗑 Удалить сервер",
    "🔗 Ссылки серверов",
    "🔑 VK аккаунты", "➕ VK аккаунт", "🗑 VK аккаунт", "🔄 VK хеши добить",
    "🗂 Тарифы", "➕ Добавить тариф", "✏️ Изменить тариф", "🗑 Удалить тариф",
    "⚙️ Админка",
}

INSTRUCTION_INTRO = "📖 <b>Подключение к WDTT</b>\n\nВыберите вашу платформу:"

INSTRUCTION_ANDROID = (
    "🤖 <b>Android</b> — установите приложение "
    "<a href='https://github.com/amurcanov/proxy-turn-vk-android/releases/download/v1.2.4/WDTT-universal.apk'>WDTT</a>, "
    "добавьте конфигурацию по ссылке и подключитесь."
)

INSTRUCTION_IOS = (
    "🍎 <b>iOS</b> — загрузите приложение "
    "<a href='https://apps.apple.com/app/testflight/id899247664'>TestFlight</a> "
    "из App Store на устройство, затем откройте "
    "<a href='https://testflight.apple.com/join/ANm6cmDv'>эту ссылку</a>, "
    "установите клиент и подключитесь, как показано на скриншотах ниже."
)

INSTRUCTION_PHOTOS = (
    ("assets/IMG_6820.jpeg", 'Откройте приложение VK Turn Proxy и нажмите внизу "Settings"'),
    ("assets/IMG_6821.jpg", 'В разделе "Backup & Restore" выберите "Import from Connection Link…"'),
    ("assets/IMG_6822.jpg", 'В открывшемся окне нажмите "Разрешить вставку" — ссылка вставится из Telegram'),
)

ANDROID_PHOTOS = (
    ("assets/IMG_6833.jpeg", 'Откройте приложение и перейдите во вкладку "Туннель"'),
    ("assets/IMG_6834.jpeg", 'Включите переключатель "Режим ссылки"'),
    ("assets/IMG_6835.jpeg", 'Вставьте вашу ссылку wdtt:// и нажмите "Подключить"'),
)

HELP_TEXT = (
    "ℹ️ <b>Как пользоваться ботом</b>\n\n"
    "1. Нажмите «🛒 Купить» и выберите тариф.\n"
    "2. Оплатите через ЮKassa по ссылке.\n"
    "3. После оплаты бот пришлёт ссылку <code>wdtt://</code> для подключения.\n\n"
    "🎁 В меню доступен бесплатный пробный период (один раз).\n"
    f"👥 Пригласите друга (кнопка 👥 Рефералы) — получите +{config.REFERRAL_REWARD_DAYS} дня "
    "после его первой оплаты.\n\n"
    "Управление — кнопками меню под полем ввода."
)


# ---------- вспомогательные ----------

def fmt_traffic(gb) -> str:
    if not gb:
        return "безлимит"
    rounded = float(gb)
    return f"{rounded:g} ГБ"


def fmt_bytes(n) -> str:
    n = int(n or 0)
    gb = n / 1024**3
    if gb >= 10:
        return f"{round(gb)} ГБ"
    if gb >= 1:
        return f"{gb:.1f} ГБ"
    mb = n / 1024**2
    if mb >= 1:
        return f"{mb:.0f} МБ"
    return f"{n} Б"


def fmt_date(ts) -> str:
    return datetime.fromtimestamp(int(ts)).strftime("%d.%m.%Y %H:%M")


def fmt_price(price) -> str:
    try:
        f = float(price)
    except (TypeError, ValueError):
        return str(price)
    return f"{int(f)}" if f == int(f) else f"{f:.2f}"


def mention(user) -> str:
    name = html.escape(user.full_name)
    return f"{name} (@{html.escape(user.username)})" if user.username else name


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


async def notify_admin(text: str) -> None:
    if not bot:
        return
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode=ParseMode.HTML)
        except Exception:
            log.exception("не удалось уведомить админа %s", admin_id)


# ---------- серверы WDTT и клиенты панели ----------

async def get_client(server_id: int | None) -> WdttClient:
    if server_id is None:
        raise WdttError("Не настроен ни один сервер WDTT (кнопка ➕ Добавить сервер)")
    if server_id in clients:
        return clients[server_id]
    server = await get_server(server_id)
    if not server:
        raise WdttError(f"Сервер WDTT #{server_id} не найден в базе")
    client = WdttClient(
        base_url=server["base_url"],
        username=server["username"],
        password=server["password"],
        verify_ssl=bool(server["verify_ssl"]),
    )
    clients[server_id] = client
    return client


async def get_default_server_id() -> int | None:
    servers = await list_servers()
    return servers[0]["id"] if servers else None


# pick_least_loaded_server_id импортируется из server_select (общий модуль с сайтом)


async def server_has_capacity(server_id: int) -> bool:
    counts = await server_active_counts()
    return counts.get(server_id, 0) < config.SERVER_MAX_USERS


async def seed_default_server() -> None:
    if await list_servers():
        return
    server_id = await add_server(
        name="main",
        base_url=config.WDTT_BASE_URL,
        username=config.WDTT_USERNAME,
        password=config.WDTT_PASSWORD,
        verify_ssl=config.WDTT_VERIFY_SSL,
    )
    # существующим клиентам без сервера назначаем сервер по умолчанию
    await backfill_customer_servers(server_id)
    log.info("Добавлен сервер по умолчанию #%s (%s)", server_id, config.WDTT_BASE_URL)


async def seed_default_plans() -> None:
    existing = await list_plans()
    if not existing:
        for p in PLANS:
            await upsert_plan(p["id"], p["name"], int(p["days"]), float(p["price"]),
                              int(p["traffic_gb"]), int(p["max_devices"]))
        log.info("Заполнены тарифы по умолчанию: %s", ", ".join(p["id"] for p in PLANS))
        return
    # ребрендинг дефолтных тарифов (Month/Year/...): меняем только name,
    # цены/дни/лимиты, которые мог поменять админ, не трогаем
    for p in PLANS:
        row = await get_plan(p["id"])
        if row and row["name"] != p["name"]:
            async with db_conn() as db:
                await db.execute("UPDATE plans SET name = ? WHERE id = ?", (p["name"], p["id"]))
            log.info("тариф %s переименован: %s -> %s", p["id"], row["name"], p["name"])


# ---------- выдача доступа ----------

def _comment_for(telegram_id: int, username: str | None) -> str:
    return f"tg:{telegram_id}" + (f" @{username}" if username else "")


def _panel_user_missing(exc: Exception) -> bool:
    """Ошибка панели означает, что пользователь в ней именно отсутствует.

    Создавать новый ключ можно только в этом случае: при таймауте/500/битом
    ответе старый ключ жив, и дубль приведёт к двум активным подпискам.
    """
    msg = str(exc).lower()
    return "not found" in msg or "не найден" in msg


async def grant_access(telegram_id: int, username: str | None, plan: dict) -> tuple[WdttClient, str, int]:
    """Создаёт или продлевает подписку. Возвращает (клиент панели, пароль, expires_at).

    Продление активной подписки продлевает тот же ключ с текущей даты окончания;
    если пользователь в панели потерялся — создаётся новый ключ.
    """
    days = int(plan["days"])
    traffic = int(plan.get("traffic_gb") or 0)
    devices = int(plan.get("max_devices") or 1)
    now = int(time.time())
    customer = await get_customer(telegram_id)

    if plan.get("addon"):
        # Аддон «больше устройств»: срок и трафик не меняем, только max_devices.
        if not (customer and customer.get("wdtt_password")):
            raise WdttError(
                "Аддон расширяет устройства только при активной подписке — "
                "сначала оформите любой тариф (🛒 Купить)."
            )
        server_id = customer["server_id"] or await get_default_server_id()
        client = await get_client(server_id)
        pwd = customer["wdtt_password"]
        expires = int(customer["expires_at"] or 0)
        info = await client.get_user_info(pwd) or {}
        vk_hash = ",".join(WdttClient._clean_hash_pieces(str(info.get("vk_hash") or "")))
        await client.update_user(old_password=pwd,
                                 expires_at=int(info.get("expires_at") or expires),
                                 total_gb=int(info.get("total_gb") or 0),
                                 max_devices=int(plan["max_devices"]),
                                 vk_hash=vk_hash)
        await upsert_customer(telegram_id, pwd, expires, customer.get("plan_id"), server_id,
                              username=username, sub_id=customer.get("sub_id"))
        await _ensure_vk_hash(client, pwd, server_id=server_id)
        return client, pwd, expires

    if customer and customer["wdtt_password"]:
        server_id = customer["server_id"] or await get_default_server_id()
        client = await get_client(server_id)
        base = max(now, int(customer["expires_at"] or 0))
        expires = base + days * 86400
        pwd = customer["wdtt_password"]
        try:
            # панель пересобирает запись: vk_hash передаём сохранённый, иначе обнулится
            try:
                info = await client.get_user_info(pwd) or {}
                vk_hash = WdttClient._clean_hash_pieces(str(info.get("vk_hash") or ""))
                vk_hash = ",".join(vk_hash)
            except Exception:
                vk_hash = ""
            await client.update_user(old_password=pwd, expires_at=expires,
                                     total_gb=traffic, max_devices=devices,
                                     vk_hash=vk_hash)
            await client.reset_traffic(pwd)
            # продление: sub_id не меняется
            await upsert_customer(telegram_id, pwd, expires, plan["id"], server_id, username=username, sub_id=customer.get("sub_id"))
        except WdttError as e:
            if not _panel_user_missing(e):
                # панель недоступна или вернула иную ошибку: ключ мог остаться
                # рабочим, поэтому новый НЕ создаём — платёж останется paid и
                # выдача повторится автоматически
                raise
            log.warning("Ключ tg %s отсутствует в панели (%s), создаём новый", telegram_id, e)
            pwd = await client.add_user(comment=_comment_for(telegram_id, username),
                                        expires_at=expires, total_gb=traffic, max_devices=devices,
                                        vk_hash=config.WDTT_VK_HASH)
            # новый ключ — генерируем новый sub_id
            sub_id = uuid.uuid4().hex[:16]
            await _store_sub_mapping(sub_id, client.base_url)
            await upsert_customer(telegram_id, pwd, expires, plan["id"], server_id, username=username, sub_id=sub_id)
    else:
        server_id = await pick_least_loaded_server_id()
        if server_id is None:
            await notify_admin(
                "🚨 Все серверы WDTT заполнены до лимита "
                f"({config.SERVER_MAX_USERS} польз.). Добавьте новый сервер (🚀 Установить сервер)."
            )
            raise WdttError(
                f"Все серверы заполнены (лимит {config.SERVER_MAX_USERS} на панель). "
                "Администратор уже уведомлён — добавьте новый сервер."
            )
        client = await get_client(server_id)
        expires = now + days * 86400
        pwd = await client.add_user(comment=_comment_for(telegram_id, username),
                                    expires_at=expires, total_gb=traffic, max_devices=devices,
                                    vk_hash=config.WDTT_VK_HASH)
        # новый пользователь — генерируем sub_id
        sub_id = uuid.uuid4().hex[:16]
        await _store_sub_mapping(sub_id, client.base_url)
        await upsert_customer(telegram_id, pwd, expires, plan["id"], server_id, username=username, sub_id=sub_id)

    await _ensure_vk_hash(client, pwd, server_id=server_id)
    return client, pwd, expires


async def _store_sub_mapping(sub_id: str, panel_base_url: str) -> None:
    """Сохраняет маппинг sub_id -> panel_base_url в Redis с TTL."""
    if not config.SUB_DOMAIN or not redis_client:
        return
    key = f"sub:{sub_id}"
    try:
        await redis_client.set(key, panel_base_url, ex=config.REDIS_SUB_TTL)
    except Exception as e:
        log.warning("Не удалось сохранить sub_id в Redis: %s", e)


async def migrate_sub_ids() -> None:
    """Генерирует sub_id для существующих активных пользователей и пишет маппинг в Redis."""
    try:
        customers = await get_customers_without_sub_id()
    except Exception:
        log.exception("migrate_sub_ids: не удалось прочитать клиентов")
        return
    if not customers:
        return
    log.info("migrate_sub_ids: найдено %d пользователей без sub_id", len(customers))
    for c in customers:
        try:
            server = await get_server(c.get("server_id") or 1)
            if not server:
                continue
            client = await get_client(server["id"])
            sub_id = uuid.uuid4().hex[:16]
            await _store_sub_mapping(sub_id, client.base_url)
            await set_customer_sub_id(c["telegram_id"], sub_id)
        except Exception:
            log.exception("migrate_sub_ids: ошибка для tg %s", c.get("telegram_id"))
    log.info("migrate_sub_ids: миграция завершена")


# Панель хранит cookies только одного VK-профиля (vk_cookies, id=1). Смена
# cookies и создание вызова обязаны быть атомарными: иначе параллельная выдача
# перетрёт cookies, и хеш будет выдан не тому VK-аккаунту, который посчитан.
_vk_cookies_lock = asyncio.Lock()


async def _pick_vk_account(server_id: int | None) -> dict | None:
    """VK-аккаунт с наименьшим числом выданных хешей (по серверу, при равенстве — всего).

    Панель хранит cookies одного профиля (vk_cookies, id=1), поэтому перед
    созданием вызова бот загружает в панель cookies выбранного профиля —
    так все VK-аккаунты админа используются на каждой панели равномерно.
    """
    accounts = await list_vk_accounts()
    if not accounts:
        return None
    counts_srv = await vk_issued_counts(server_id) if server_id else {}
    counts_all = await vk_issued_counts(None)
    return min(
        accounts,
        key=lambda a: (counts_srv.get(a["id"], 0), counts_all.get(a["id"], 0), a["id"]),
    )


async def _create_vk_hash_rotated(client: WdttClient, server_id: int, pwd: str) -> str | None:
    """Создаёт один VK-вызов для пользователя, используя наименее загруженный VK-профиль.

    Перед вызовом загружает cookies этого профиля в панель (POST vk/cookies)
    и фиксирует выданный хеш в vk_issued. Возвращает хеш или None при сбое.
    """
    account = await _pick_vk_account(server_id)
    if not account:
        return None
    try:
        async with _vk_cookies_lock:
            await client.set_vk_cookies(account["cookies"])
            vk_hash = await client.create_vk_call(pwd)
        for piece in WdttClient._clean_hash_pieces(vk_hash):
            await record_vk_issued(server_id, account["id"], pwd, piece)
        return vk_hash
    except Exception as e:
        log.warning("VK-вызов через аккаунт #%s (%s) не удался: %s", account["id"], account["label"], e)
        return None


async def _ensure_vk_hash(client: WdttClient, pwd: str, server_id: int | None = None,
                          target: int | None = None) -> None:
    """Гарантирует до VK_HASHES_PER_USER (по умолчанию 4) vk_hash у пользователя.

    - если задан WDTT_VK_HASH — ничего не делает (общий хеш задаётся при создании);
    - добивает недостающие хеши до target (4), каждый — с наименее загруженного
      VK-аккаунта (ротация cookies в панели + запись в vk_issued);
    - панель сама ограничивает максимум в 4 звонка на профиль (vkhash.Max).

    Сбой не прерывает выдачу — в ссылках останется плейсхолдер VK_HASH (не фатально).
    """
    if config.WDTT_VK_HASH:
        return
    target = target or config.VK_HASHES_PER_USER
    try:
        info = await client.get_user_info(pwd) or {}
        existing = WdttClient._clean_hash_pieces(str(info.get("vk_hash") or ""))
        missing = max(0, target - len(existing))
        if missing <= 0:
            return  # уже есть 4 (или больше) настоящих хеша

        new_hashes: list[str] = []
        for _ in range(missing):
            h = await _create_vk_hash_rotated(client, server_id or 0, pwd)
            if not h:
                break
            new_hashes.extend(WdttClient._clean_hash_pieces(h))
        if not new_hashes:
            log.warning("VK-хеши для %s не созданы (нет рабочих VK-аккаунтов?)", pwd[:2])
            return
        # панель вернула полный CSV после apply — перечитываем, чтобы не потерять старые
        info2 = await client.get_user_info(pwd) or {}
        merged_pieces = WdttClient._clean_hash_pieces(str(info2.get("vk_hash") or ""))
        if not merged_pieces:
            merged_pieces = existing + new_hashes
        merged = ",".join(merged_pieces[:target])
        await client.update_user(old_password=pwd, password="",
                                 expires_at=int(info.get("expires_at") or 0),
                                 total_gb=int(info.get("total_gb") or 0),
                                 max_devices=int(info.get("max_devices") or 1),
                                 vk_hash=merged)
    except Exception as e:
        log.warning("VK-хеши для %s не созданы (%s) — hash останется плейсхолдером", pwd[:2], e)


async def backfill_vk_hashes(max_users: int = 200) -> dict:
    """Добивает VK-хеши до 4 всем активным клиентам, у кого их меньше.

    Запускается фоном при старте и по команде админа «🔄 VK хеши добить».
    Возвращает статистику {"checked", "topped", "failed"}.
    """
    stats = {"checked": 0, "topped": 0, "failed": 0}
    if config.WDTT_VK_HASH:
        return stats  # общий хеш из конфига — создавать вызовы не нужно
    customers = await get_active_customers()
    for c in customers[:max_users]:
        pwd = c.get("wdtt_password")
        if not pwd:
            continue
        server_id = c.get("server_id") or await get_default_server_id()
        if server_id is None:
            continue
        stats["checked"] += 1
        try:
            client = await get_client(server_id)
            info = await client.get_user_info(pwd) or {}
            existing = WdttClient._clean_hash_pieces(str(info.get("vk_hash") or ""))
            if len(existing) >= config.VK_HASHES_PER_USER:
                continue
            await _ensure_vk_hash(client, pwd, server_id=server_id)
            info2 = await client.get_user_info(pwd) or {}
            now_count = len(WdttClient._clean_hash_pieces(str(info2.get("vk_hash") or "")))
            if now_count > len(existing):
                stats["topped"] += 1
        except Exception as e:
            stats["failed"] += 1
            log.warning("backfill: tg %s — ошибка: %s", c.get("telegram_id"), e)
    log.info("backfill_vk_hashes завершён: %s", stats)
    return stats


async def grant_referral_reward(referred_id: int) -> None:
    """+N дней пригласившему после первой оплаты приглашённого.

    claim_referral_reward только выбирает необработанного реферала;
    пометка «rewarded» ставится (mark_referral_rewarded) после успешного
    применения, так что сбой не сжигает бонус — он будет применён при
    следующей оплате приглашённого.
    """
    days = config.REFERRAL_REWARD_DAYS
    if days <= 0:
        return
    referrer_id = await claim_referral_reward(referred_id)
    if not referrer_id:
        return
    referrer = await get_customer(referrer_id)
    now = int(time.time())
    if not referrer or not referrer["wdtt_password"] or int(referrer["expires_at"] or 0) <= now:
        # бонус не сгорает: ждём, пока пригласивший обновит подписку
        try:
            await bot.send_message(
                referrer_id,
                "🎉 Ваш друг оплатил подписку! К сожалению, бонусные дни начисляются "
                f"только на активную подписку — продлите свою (кнопка 🛒 Купить), чтобы не терять их.",
            )
        except Exception:
            pass
        return
    try:
        server_id = referrer["server_id"] or await get_default_server_id()
        client = await get_client(server_id)
        pwd = referrer["wdtt_password"]
        info = await client.get_user_info(pwd) or {}
        expires = max(now, int(referrer["expires_at"])) + days * 86400
        total_gb = int(info.get("total_gb") or 0)
        max_devices = int(info.get("max_devices") or 1)
        # 0 ГБ = безлимит — не превращаем его в лимит
        if config.REFERRAL_REWARD_TRAFFIC_GB > 0 and total_gb > 0:
            total_gb += config.REFERRAL_REWARD_TRAFFIC_GB
        if config.REFERRAL_REWARD_MAX_DEVICES > max_devices:
            max_devices = config.REFERRAL_REWARD_MAX_DEVICES
        await client.update_user(old_password=pwd, expires_at=expires,
                                 total_gb=total_gb, max_devices=max_devices)
        await upsert_customer(referrer_id, pwd, expires, referrer["plan_id"], server_id,
                              username=referrer["username"])
        if not await mark_referral_rewarded(referred_id):
            log.warning("реферал tg %s уже был помечен — бонус мог быть применён раньше", referred_id)
        await bot.send_message(
            referrer_id,
            f"🎁 Бонус за друга: +{days} дн. к вашей подписке! Действует до {fmt_date(expires)}.",
        )
    except Exception:
        log.exception("Не удалось начислить реферальный бонус %s -> %s", referred_id, referrer_id)
        await notify_admin(f"⚠️ Не удалось начислить реферальный бонус (tg {referrer_id}).")


# ---------- клавиатуры ----------

async def nav_menu(telegram_id: int, is_admin_user: bool | None = None) -> ReplyKeyboardMarkup:
    """Главное меню. Кнопка «Пробный период» появляется, только если пробник
    ещё не использован; админские кнопки спрятаны за «⚙️ Админка»."""
    if is_admin_user is None:
        is_admin_user = is_admin(telegram_id)
    rows = []
    if not await has_used_trial(telegram_id):
        rows.append([KeyboardButton(text="🎁 Пробный период"), KeyboardButton(text="🛒 Купить")])
    else:
        rows.append([KeyboardButton(text="🛒 Купить")])
    rows += [
        [KeyboardButton(text="👤 Моя подписка"), KeyboardButton(text="👥 Рефералы")],
        [KeyboardButton(text="📖 Инструкция"), KeyboardButton(text="❓ Помощь")],
    ]
    if is_admin_user:
        rows[-1].append(KeyboardButton(text="⚙️ Админка"))
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите действие из меню",
    )


def admin_menu() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="👥 Клиенты")],
        [KeyboardButton(text="⛔ Отозвать"), KeyboardButton(text="➕ Продлить")],
        [KeyboardButton(text="🎁 Подарить"), KeyboardButton(text="🖥 Серверы")],
        [KeyboardButton(text="➕ Добавить сервер"), KeyboardButton(text="🚀 Установить сервер")],
        [KeyboardButton(text="🗑 Удалить сервер"), KeyboardButton(text="🔗 Ссылки серверов")],
        [KeyboardButton(text="🔑 VK аккаунты"), KeyboardButton(text="➕ VK аккаунт")],
        [KeyboardButton(text="🗑 VK аккаунт"), KeyboardButton(text="🔄 VK хеши добить")],
        [KeyboardButton(text="🗂 Тарифы"), KeyboardButton(text="➕ Добавить тариф")],
        [KeyboardButton(text="✏️ Изменить тариф"), KeyboardButton(text="🗑 Удалить тариф")],
        [KeyboardButton(text="🏠 Главное меню")],
    ]
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Админ-панель",
    )


def plans_reply_keyboard(plans) -> ReplyKeyboardMarkup:
    PLAN_BUTTONS.clear()
    rows = []
    for p in plans:
        text = f"💳 {p['name']} — {fmt_price(p['price'])} ₽"
        PLAN_BUTTONS[text] = p["id"]
        rows.append([KeyboardButton(text=text)])
    # аддоны на расширение устройств — отдельным блоком
    for p in DEVICE_ADDONS:
        text = f"➕ {p['name']} — +{fmt_price(p['price'])} ₽"
        PLAN_BUTTONS[text] = p["id"]
        rows.append([KeyboardButton(text=text)])
    rows.append([KeyboardButton(text="🏠 Главное меню")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
    )


# ---------- профиль и статус ----------

async def _links_section(client, pwd: str, sub_id: str | None = None) -> str | None:
    """Блок со ссылками подключения для iOS и Android.

    Если настроен SUB_DOMAIN и есть sub_id — отдаём единую ссылку на подписку.
    Иначе платформенные ссылки берём со страницы подписки панели; если их нет —
    откатываемся на универсальную wdtt:// ссылку.
    """
    # Если есть единый домен подписки и sub_id — отдаём унифицированную ссылку,
    # но только если Redis доступен (иначе nginx не найдёт маппинг)
    if config.SUB_DOMAIN and sub_id and redis_client:
        try:
            mapped = await redis_client.get(f"sub:{sub_id}")
            if mapped:
                sub_path = config.SUB_PATH.rstrip("/")
                unified_url = f"{config.SUB_DOMAIN.rstrip('/')}{sub_path}/{sub_id}"
                return f"<b>🔗 Единая ссылка подписки:</b>\n<code>{html.escape(unified_url)}</code>"
        except Exception as e:
            log.warning("Redis недоступен при проверке sub_id, fallback на прямые ссылки: %s", e)

    try:
        pl = await client.platform_links(pwd)
    except Exception:
        log.warning("платформенные ссылки недоступны, используем универсальную")
        pl = {}
    labels = (("ios", "🍏 iOS"), ("android", "🤖 Android"))
    parts = [f"<b>{label}</b>\n<code>{html.escape(pl[key])}</code>"
             for key, label in labels if pl.get(key)]
    if parts:
        return "\n\n".join(parts)
    try:
        univers = await client.find_link(pwd)
    except Exception:
        return None
    return f"<code>{html.escape(univers)}</code>" if univers else None


async def links_json(client, pwd: str, sub_id: str | None = None) -> dict:
    """Ссылки подключения в JSON для Mini App: {unified?, ios?, android?, universal?}."""
    out: dict = {}
    if config.SUB_DOMAIN and sub_id and redis_client:
        try:
            mapped = await redis_client.get(f"sub:{sub_id}")
            if mapped:
                sub_path = config.SUB_PATH.rstrip("/")
                out["unified"] = f"{config.SUB_DOMAIN.rstrip('/')}{sub_path}/{sub_id}"
        except Exception as e:
            log.warning("Redis недоступен при проверке sub_id: %s", e)
    if "unified" not in out:
        try:
            pl = await client.platform_links(pwd)
            if pl.get("ios"):
                out["ios"] = pl["ios"]
            if pl.get("android"):
                out["android"] = pl["android"]
        except Exception:
            pass
        if not out:
            try:
                u = await client.find_link(pwd)
                if u:
                    out["universal"] = u
            except Exception:
                pass
    return out


async def profile_text(telegram_id: int) -> str:
    customer = await get_customer(telegram_id)
    now = int(time.time())
    if not customer or not customer["wdtt_password"]:
        return "У вас нет активной подписки. Нажмите 🛒 Купить или активируйте 🎁 пробный период."
    expires_at = int(customer["expires_at"] or 0)
    if expires_at <= now:
        return "Ваша подписка истекла. Продлить: кнопка 🛒 Купить"

    plan = await get_plan(customer["plan_id"]) or ALL_PLANS_BY_ID.get(customer["plan_id"])
    plan_name = plan["name"] if plan else customer["plan_id"] or "—"
    diff = expires_at - now
    days, hours = diff // 86400, (diff % 86400) // 3600

    used_bytes = 0
    used_bytes = 0
    section = None
    try:
        client = await get_client(customer["server_id"])
        info = await client.get_user_info(customer["wdtt_password"])
        if info:
            used_bytes = int(info.get("up_bytes") or 0) + int(info.get("down_bytes") or 0)
        section = await _links_section(client, customer["wdtt_password"], customer.get("sub_id"))
    except Exception:
        log.warning("не удалось получить ссылку для tg %s", telegram_id)

    lines = [
        f"📊 <b>Ваша подписка:</b> {html.escape(str(plan_name))}",
        f"📅 Действует до: {fmt_date(expires_at)}",
        f"⏳ Осталось: {days} дн. {hours} ч",
    ]
    if plan:
        limit_gb = plan.get("traffic_gb")
        traffic = (f"{fmt_bytes(used_bytes)} из {fmt_traffic(limit_gb)}" if limit_gb
                   else f"{fmt_bytes(used_bytes)} (безлимит)")
        lines.append(f"📶 Трафик: {traffic} · Устройств: {plan.get('max_devices', 1)}")
    if section:
        lines.append(f"\n🔗 Ссылки для подключения:\n{section}")
    else:
        lines.append("\n🔗 Ссылка временно недоступна — попробуйте позже (кнопка 👤 Моя подписка).")
    return "\n".join(lines)


# ---------- старт (единственный технический обработчик команды) ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    # реферальная ссылка: https://t.me/bot?start=ref_<tg_id>
    # привязка сайта: https://t.me/bot?start=bind_<code>
    args = (message.text or "").split()
    if len(args) > 1:
        if args[1].startswith("ref_"):
            raw = args[1][4:]
            if raw.isdigit() and int(raw) != message.from_user.id:
                await add_referral(message.from_user.id, int(raw))
        elif args[1].startswith("bind_"):
            code = args[1][5:]
            await _bind_site_code(message, code)
        elif args[1].startswith("web_login_"):
            code = args[1][10:]
            await _web_login_confirm(message, code)
            return
    await state.clear()  # сбрасываем любой незавершённый FSM-диалог

    # Бот — только точка входа: встречаем пользователя и отправляем в приложение.
    # Всё остальное (подписка, оплата, настройки, рефералы) живёт в Mini App.
    # Кнопка «Сайт» — одноразовая ссылка автовхода: переход на сайт из Telegram
    # сразу открывает профиль, без ручного входа.
    site_url = ""
    if config.SITE_URL and message.from_user:
        try:
            code = secrets.token_urlsafe(24)
            await create_auto_login(code, message.from_user.id, message.from_user.username)
            site_url = f"{config.SITE_URL.rstrip('/')}/login/auto?code={code}"
        except Exception:
            log.warning("не удалось создать автовход на сайт", exc_info=True)
            site_url = config.SITE_URL
    if config.WEBAPP_ENABLED and config.MINIAPP_URL:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🚀 Открыть приложение",
                web_app=WebAppInfo(url=config.MINIAPP_URL),
            )],
            ([InlineKeyboardButton(text="🌐 Сайт K.O. Gateway (войти автоматически)", url=site_url)]
             if site_url else []),
        ])
        await message.answer(
            "👋 Привет! Это <b>K.O. Gateway</b>.\n\n"
            "Нажмите кнопку ниже — откроется приложение: оплата подписки, пробный период, "
            "ссылки для подключения, реферальная программа и настройки.",
            reply_markup=kb,
        )
        return

    await message.answer(
        "👋 Привет! Это бот продажи доступа к <b>WDTT</b>.\n\n"
        "Здесь можно купить подписку (оплата через ЮKassa), активировать "
        "бесплатный пробный период и получить ссылку для подключения.\n\n"
        "Выберите действие:",
        reply_markup=await nav_menu(message.from_user.id),
    )


@router.message(F.text.startswith("/bind"))
async def cmd_bind(message: Message) -> None:
    """Привязка аккаунта сайта к Telegram: /bind <код> (или deep-link ?start=bind_<код>)."""
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Использование: /bind <код_привязки> — код показан на сайте в разделе «Привязка Telegram», "
                             "там же есть кнопка-ссылка на этого бота.")
        return
    await _bind_site_code(message, parts[1])


async def _bind_site_code(message: Message, code: str) -> None:
    """Привязка сайта по коду: фиксируем связь site_user <-> telegram_id в БД.

    Общий путь для /bind <код> и deep-link https://t.me/<bot>?start=bind_<код>:
    берём запись bind_codes, находим site_user, ставим telegram_id,
    помечаем код использованным и создаём/обновляем запись в customers —
    после этого подписка видна и на сайте, и в боте.
    """
    code = (code or "").strip()
    await init_db()
    bind_row = await get_bind_code(code)
    if not bind_row:
        await message.answer("❌ Код привязки не найден, уже использован или неверен. "
                             "Получите новый код на сайте в разделе «Привязка Telegram».")
        return
    site_user = await get_site_user_by_id(bind_row["site_user_id"])
    if not site_user:
        await message.answer("❌ Аккаунт на сайте не найден.")
        return
    ok = await bind_site_telegram(site_user["id"], message.from_user.id)
    if not ok:
        await message.answer(
            "⚠️ Этот аккаунт на сайте уже привязан к другому Telegram или код уже использован."
        )
        return
    await mark_bind_code_used(code)
    # фиксируем пользователя Telegram в базе (customers), чтобы подписка
    # была привязана и работала выдача/продление через бота
    tg_id = message.from_user.id
    async with db_conn() as db:
        # если подписка была куплена на сайте без привязки (псевдо-id),
        # переносим её на реальный telegram_id
        old_id = -abs(int(site_user["id"]))
        cur = await db.execute("SELECT telegram_id FROM customers WHERE telegram_id = ?", (tg_id,))
        already = await cur.fetchone()
        if not already:
            cur = await db.execute(
                "SELECT telegram_id FROM customers WHERE telegram_id = ?", (old_id,)
            )
            old_row = await cur.fetchone()
            if old_row:
                await db.execute(
                    "UPDATE customers SET telegram_id = ?, username = COALESCE(NULLIF(?, ''), username) "
                    "WHERE telegram_id = ?",
                    (tg_id, message.from_user.username or message.from_user.first_name or "", old_id),
                )
                await db.execute("UPDATE payments SET telegram_id = ? WHERE telegram_id = ?", (tg_id, old_id))
                await db.execute("UPDATE trials SET telegram_id = ? WHERE telegram_id = ?", (tg_id, old_id))
        await db.execute(
            "INSERT INTO customers (telegram_id, username, expires_at, plan_id, server_id, created_at) "
            "VALUES (?, ?, 0, NULL, NULL, ?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET username=COALESCE(excluded.username, customers.username)",
            (tg_id, message.from_user.username or message.from_user.first_name or "",
             int(time.time())),
        )
    await message.answer(
        "✅ Telegram привязан к аккаунту на сайте "
        f"(<b>{html.escape(site_user.get('email') or '')}</b>).\n"
        "Теперь вы видите подписку и на сайте, и в боте — покупка и пробный период "
        "работают в обоих местах."
    )


async def _web_login_confirm(message: Message, code: str) -> None:
    """Вход на сайт через Telegram: пользователь нажал кнопку на сайте.

    Сайт создал код (login_requests), бот подтверждает его своим telegram_id
    и выдаёт кнопку «Войти на сайт» с одноразовой ссылкой.
    """
    code = (code or "").strip()
    if not message.from_user:
        return
    await init_db()
    ok = await confirm_login_request(
        code,
        message.from_user.id,
        message.from_user.username or message.from_user.first_name or "",
    )
    if not ok:
        await message.answer(
            "❌ Ссылка входа недействительна или уже использована.\n"
            "Откройте сайт заново и нажмите «Войти через Telegram»."
        )
        return
    site = (config.SITE_URL or "").rstrip("/")
    if not site:
        await message.answer(
            "⚠️ Вход на сайт временно недоступен: не задан SITE_URL на сервере."
        )
        return
    await message.answer(
        "✅ Вход подтверждён. Нажмите кнопку ниже, чтобы войти на сайт "
        f"(<b>{html.escape(site)}</b>).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="🔐 Войти на сайт",
                url=f"{site}/auth/telegram/confirm?code={code}",
            ),
        ]]),
    )


# ---------- Telegram Stars ----------

@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    """Подтверждаем pre-checkout для Stars (всегда ок — валидация будет в successful_payment)."""
    try:
        await query.answer(True)
    except Exception:
        log.exception("pre_checkout answer")


@router.message(F.successful_payment)
async def on_successful_payment(message: Message) -> None:
    """Оплата Telegram Stars прошла — выдаём доступ по invoice_payload."""
    sp = message.successful_payment
    if not sp or sp.currency != "XTR":
        return
    charge_id = sp.telegram_payment_charge_id
    payment_id = f"stars:{charge_id}"
    try:
        payload = json.loads(sp.invoice_payload or "{}")
    except ValueError:
        payload = {}
    plan_id = str(payload.get("plan_id") or "")
    telegram_id = int(payload.get("tg") or message.from_user.id)
    plan = await get_plan(plan_id) or ALL_PLANS_BY_ID.get(plan_id)
    if not plan:
        log.warning("Stars: неизвестный тариф %s в payload %s", plan_id, sp.invoice_payload)
        return
    # защита от дубля
    existing = await get_payment(payment_id)
    if existing and existing["status"] in ("paid", "fulfilled"):
        return
    await add_payment(telegram_id, message.from_user.username, payment_id, plan_id,
                      f"{sp.total_amount} XTR")
    await set_payment_status(payment_id, "paid", paid_at=int(time.time()))
    try:
        client, pwd, expires = await grant_access(telegram_id, message.from_user.username, plan)
        await set_payment_status(payment_id, "fulfilled")
        customer = await get_customer(telegram_id)
        links = await links_json(client, pwd, (customer or {}).get("sub_id"))
        links_html = ""
        if links:
            parts = [f"<code>{html.escape(v)}</code>" for v in links.values()]
            links_html = "\n🔗 Ссылки:\n" + "\n".join(parts) + "\n"
        if plan.get("addon"):
            await message.answer(
                f"✅ <b>Оплата звёздами прошла!</b> ({sp.total_amount} ⭐)\n\n"
                f"Теперь можно подключить до <b>{plan['max_devices']} устройств</b> "
                f"(тариф «{html.escape(plan['name'])}»). Срок подписки не изменился: до {fmt_date(expires)}.\n"
                f"{links_html}\nПриложение: кнопка «🚀 Открыть приложение» в /start"
            )
        else:
            await message.answer(
                f"✅ <b>Оплата звёздами прошла!</b> ({sp.total_amount} ⭐)\n\n"
                f"Подписка «{html.escape(plan['name'])}» активирована до {fmt_date(expires)}.\n"
                f"{links_html}\nПриложение: кнопка «🚀 Открыть приложение» в /start"
            )
        await notify_admin(
            f"⭐ Оплата Stars: {sp.total_amount} ⭐ — {html.escape(plan['name'])}\n"
            f"Клиент: {telegram_id}"
            + (f" (@{html.escape(message.from_user.username)})" if message.from_user.username else "")
        )
        await grant_referral_reward(telegram_id)
        import metrics as m
        m.inc_counter(m.PAYMENTS_TOTAL, status="succeeded", method="stars")
    except Exception as e:
        log.exception("Stars: ошибка выдачи доступа %s", payment_id)
        await notify_admin(
            f"🚨 <b>Stars-ошибка выдачи</b>\nПлатёж: <code>{payment_id}</code>\n"
            f"Клиент: {telegram_id}\nПричина: <code>{html.escape(str(e))}</code>"
        )


async def ref_text(telegram_id: int) -> str:
    username = bot_username or "bot"
    link = f"https://t.me/{username}?start=ref_{telegram_id}"
    count = await get_referrals_count(telegram_id)
    return (
        f"👥 <b>Ваша реферальная ссылка:</b>\n{link}\n\n"
        f"Пригласите друга — после его первой оплаты вы получите "
        f"+{config.REFERRAL_REWARD_DAYS} дн. к подписке.\n"
        f"Приглашено: {count}"
    )


# ---------- кнопки клиента ----------

@router.message(F.text == "🛒 Купить", StateFilter(None))
async def nav_buy(message: Message) -> None:
    await message.answer("Выберите тариф:", reply_markup=plans_reply_keyboard(await list_plans()))


@router.message(F.text == "👤 Моя подписка", StateFilter(None))
async def nav_profile(message: Message) -> None:
    await message.answer(await profile_text(message.from_user.id))


@router.message(F.text == "👥 Рефералы", StateFilter(None))
async def nav_ref(message: Message) -> None:
    await message.answer(await ref_text(message.from_user.id))


@router.message(F.text == "📖 Инструкция", StateFilter(None))
async def nav_instruction(message: Message) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Android", callback_data="instr:android:0"),
         InlineKeyboardButton(text="🍎 iOS", callback_data="instr:ios:0")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="instr:main")],
    ])
    await message.answer(INSTRUCTION_INTRO, reply_markup=kb)


@router.callback_query(F.data.startswith("instr:"))
async def instr_callback(cb: CallbackQuery) -> None:
    if not cb.message:
        await cb.answer()
        return
    parts = cb.data.split(":")
    if parts[1] == "main":
        await cb.message.delete()
        await cb.message.answer("Главное меню:", reply_markup=await nav_menu(cb.from_user.id))
        await cb.answer()
        return

    platform = parts[1]  # android / ios
    step = int(parts[2])

    if platform == "android":
        photos = ANDROID_PHOTOS
        intro = INSTRUCTION_ANDROID
    else:
        photos = INSTRUCTION_PHOTOS
        intro = INSTRUCTION_IOS

    if step == 0:
        # первый экран — текст + кнопка «Начать»
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Начать ▶", callback_data=f"instr:{platform}:1")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="instr:main")],
        ])
        await cb.message.edit_text(intro, reply_markup=kb, parse_mode=ParseMode.HTML)
        await cb.answer()
        return

    # шаги с фото (1-based)
    if step > len(photos):
        await cb.answer()
        return

    path, caption = photos[step - 1]
    buttons = []
    if step > 1:
        buttons.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"instr:{platform}:{step - 1}"))
    if step < len(photos):
        buttons.append(InlineKeyboardButton(text="Далее ▶", callback_data=f"instr:{platform}:{step + 1}"))
    else:
        buttons.append(InlineKeyboardButton(text="🏠 Главное меню", callback_data="instr:main"))

    kb = InlineKeyboardMarkup(inline_keyboard=[buttons])
    await cb.message.delete()
    await cb.message.answer_photo(FSInputFile(path), caption=caption, reply_markup=kb, parse_mode=ParseMode.HTML)
    await cb.answer()


# ---------- старые хендлеры кнопок меню (оставлены для совместимости, но не используются) ----------
@router.message(F.text == "🤖 Android", StateFilter(None))
async def nav_instr_android(message: Message) -> None:
    await message.answer("Откройте инструкцию через кнопку 📖 Инструкция → 🤖 Android")


@router.message(F.text == "🍎 iOS", StateFilter(None))
async def nav_instr_ios(message: Message) -> None:
    await message.answer("Откройте инструкцию через кнопку 📖 Инструкция → 🍎 iOS")


@router.message(F.text == "❓ Помощь", StateFilter(None))
async def nav_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(F.text == "🏠 Главное меню", StateFilter(None))
async def nav_main(message: Message) -> None:
    await message.answer("Главное меню:", reply_markup=await nav_menu(message.from_user.id))


@router.message(F.text == "⚙️ Админка", StateFilter(None))
async def nav_admin_menu(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.answer("Админ-панель:", reply_markup=admin_menu())


@router.message(F.text == "🎁 Пробный период", StateFilter(None))
async def nav_trial(message: Message) -> None:
    user_id = message.from_user.id
    customer = await get_customer(user_id)
    now = int(time.time())
    if customer and customer["wdtt_password"] and int(customer["expires_at"] or 0) > now:
        await message.answer("У вас уже есть активная подписка!")
        return
    # атомарно занимаем слот до вызова панели — двойная заявка безопасна;
    # при провале выдачи слот освобождается, можно повторить
    if not await claim_trial(user_id):
        await message.answer("Вы уже использовали пробный период!")
        return

    try:
        client, pwd, _ = await grant_access(user_id, message.from_user.username, TRIAL)
    except Exception:
        log.exception("ошибка выдачи пробного периода tg %s", user_id)
        await release_trial(user_id)
        await message.answer("⚠️ Не удалось создать пробный период. Попробуйте ещё раз.")
        return

    section = await _links_section(client, pwd)
    await message.answer(
        "🎁 <b>Пробный период активирован!</b>\n\n"
        f"Срок: {TRIAL['days']} дн. · Трафик: {fmt_traffic(TRIAL['traffic_gb'])} · "
        f"Устройств: {TRIAL['max_devices']}\n\n"
        + (f"Ссылки для подключения:\n{section}\n\n" if section else "")
        + "Инструкция — кнопка 📖 Инструкция."
    )


@router.message(
    (F.text.startswith("💳 ") | F.text.startswith("➕ "))
    & ~F.text.in_(ADMIN_NAV_BUTTONS),
    StateFilter(None),
)
async def nav_plan(message: Message) -> None:
    plan_id = PLAN_BUTTONS.get(message.text or "")
    plan = await get_plan(plan_id) if plan_id else None
    if not plan:
        plan = ALL_PLANS_BY_ID.get(plan_id or "")
    if not plan:
        await message.answer("Тариф не найден. Нажмите 🛒 Купить, чтобы показать актуальные тарифы.")
        return
    try:
        payment = await create_payment(plan, message.from_user.id)
    except Exception:
        log.exception("не удалось создать платёж (тариф %s)", plan_id)
        await message.answer("Не удалось создать платёж. Попробуйте позже.")
        return
    if not payment.get("url"):
        await message.answer("ЮKassa не вернула ссылку на оплату. Попробуйте позже.")
        return

    await add_payment(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        payment_id=payment["id"],
        plan_id=str(plan["id"]),
        amount=f"{float(plan['price']):.2f}",
    )
    if plan.get("addon"):
        detail = f"Устройств: до {plan['max_devices']} · Срок подписки не меняется"
    else:
        detail = f"Срок: {plan['days']} дн. · Трафик: {fmt_traffic(plan.get('traffic_gb'))} · Устройств: до {plan.get('max_devices', 1)}"
    await message.answer(
        f"💳 <b>Оплата тарифа «{html.escape(plan['name'])}»</b>\n\n"
        f"Сумма: {fmt_price(plan['price'])} ₽\n"
        f"{detail}\n\n"
        f"🔗 Ссылка для оплаты:\n{html.escape(payment['url'])}\n\n"
        "Оплатите по ссылке — после подтверждения доступ придёт автоматически "
        "(обычно в течение минуты).\n"
        + ("" if plan.get("addon") else "Если вы уже платили — подписка продлится с текущей даты окончания.")
    )


@router.message(~StateFilter(None), F.text.in_(NAV_BUTTONS))
async def nav_button_during_state(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.text == "🏠 Главное меню":
        await message.answer("Главное меню:", reply_markup=await nav_menu(message.from_user.id))
    else:
        await message.answer("Действие отменено. Нажмите кнопку ещё раз.")


# ---------- исполнение платежей ----------

async def _access_already_granted(telegram_id: int, plan: dict) -> bool:
    """True, если подписка в панели уже продлена на срок этого платежа.

    Страховка на случай падения между grant_access и записью «fulfilled»:
    без неё поллер повторно продлил бы подписку за одну оплату.
    """
    cust = await get_customer(telegram_id)
    if not cust or not cust["wdtt_password"]:
        return False
    if plan.get("addon"):
        return False  # аддон не продлевает срок — проверка по сроку не применима
    try:
        client = await get_client(cust["server_id"])
        info = await client.get_user_info(cust["wdtt_password"]) or {}
    except Exception as e:
        log.info("не удалось проверить текущий срок tg %s: %s", telegram_id, e)
        return False
    panel_expires = int(info.get("expires_at") or 0)
    now = int(time.time())
    if panel_expires <= now:
        return False
    db_expires = int(cust["expires_at"] or 0)
    expected = max(now, db_expires) + int(plan["days"]) * 86400
    # панель могла выдать срок чуть раньше наших вычислений — допуск 10 минут
    return panel_expires + 600 >= expected


async def fulfill_payment(payment_id: str) -> None:
    async with fulfill_lock:
        row = await get_payment(payment_id)
        if not row or row["status"] not in ("pending", "paid"):
            return

        try:
            yk = await fetch_payment(payment_id)
        except Exception as e:
            log.warning("ЮKassa недоступна при проверке %s: %s", payment_id, e)
            return
        if yk is None:
            await set_payment_status(payment_id, "cancelled")
            return
        if yk["status"] == "canceled":
            await set_payment_status(payment_id, "cancelled")
            return
        if yk["status"] != "succeeded":
            return  # ещё не оплачен — проверим на следующей итерации

        # сверяем сумму и метаданные с нашей записью (защита от чужих платежей
        # на том же магазине и от подменённых webhook-уведомлений)
        try:
            yk_amount = float(yk.get("amount") or 0)
        except (TypeError, ValueError):
            yk_amount = -1.0
        if abs(yk_amount - float(row["amount"])) > 0.01:
            await notify_admin(
                f"🚨 <b>Платёж {payment_id}</b>: сумма в ЮKassa ({yk_amount:.2f} ₽) "
                f"не совпадает с записью ({fmt_price(row['amount'])} ₽) — выдача прервана."
            )
            return
        yk_tg = (yk.get("metadata") or {}).get("telegram_id")
        if yk_tg and str(yk_tg) != str(row["telegram_id"]):
            await notify_admin(
                f"🚨 <b>Платёж {payment_id}</b>: metadata.telegram_id={yk_tg}, "
                f"в записи — {row['telegram_id']} — выдача прервана."
            )
            return

        if row["status"] == "pending":
            await set_payment_status(payment_id, "paid", paid_at=yk.get("paid_at"))

        telegram_id = int(row["telegram_id"])

        # сохраняем способ оплаты для автопродления (ЮKassa вернёт payment_method_id после оплаты картой)
        pm_id = yk.get("payment_method_id")
        if pm_id:
            try:
                await set_payment_method(telegram_id, str(pm_id))
            except Exception as e:
                log.warning("не удалось сохранить payment_method_id для tg %s: %s", telegram_id, e)

        plan = await get_plan(row["plan_id"]) or ALL_PLANS_BY_ID.get(row["plan_id"])
        if not plan:
            await notify_admin(
                f"⚠️ Платёж {payment_id} оплачен, но тариф «{row['plan_id']}» не найден — "
                f"доступ клиенту {telegram_id} не выдан."
            )
            return

        # идемпотентность при повторе после сбоя: если подписка в панели уже
        # продлена на срок этого платежа — не выдаём второй раз
        if await _access_already_granted(telegram_id, plan):
            log.info("платёж %s уже выдан (по сроку в панели) — пропускаем", payment_id)
            if row["status"] != "fulfilled":
                await set_payment_status(payment_id, "fulfilled")
            return

        try:
            client, pwd, expires = await grant_access(telegram_id, row.get("username"), plan)
        except Exception as e:
            log.exception("ошибка выдачи доступа по платёж %s", payment_id)
            await notify_admin(
                f"🚨 <b>Ошибка выдачи доступа</b>\nПлатёж: <code>{payment_id}</code>\n"
                f"Клиент: {telegram_id}\nПричина: <code>{html.escape(str(e))}</code>\n\n"
                "Платёж в статусе paid — бот повторит выдачу автоматически."
            )
            return

        await set_payment_status(payment_id, "fulfilled")

        # метрики
        import metrics as m
        m.inc_counter(m.PAYMENTS_TOTAL, status="succeeded")
        try:
            m.inc_counter(m.PAYMENTS_AMOUNT_RUB, float(row["amount"]))
        except (TypeError, ValueError):
            pass

        section = None
        try:
            section = await _links_section(client, pwd)
        except Exception:
            log.warning("не удалось получить ссылку после оплаты %s", payment_id)
        if plan.get("addon"):
            message_text = (
                "✅ <b>Оплата прошла успешно!</b>\n\n"
                f"Теперь можно подключить до <b>{plan['max_devices']} устройств</b> "
                f"(тариф «{html.escape(plan['name'])}»).\nСрок подписки не изменился: до {fmt_date(expires)}.\n"
                + (f"\n🔗 Ссылки для подключения:\n{section}\n" if section else "")
                + "\nИнструкция по подключению — кнопка 📖 Инструкция"
            )
        else:
            message_text = (
                "✅ <b>Оплата прошла успешно!</b>\n\n"
                f"Подписка «{html.escape(plan['name'])}» активирована до {fmt_date(expires)}.\n"
                + (f"\n🔗 Ссылки для подключения:\n{section}\n" if section else "")
                + "\nИнструкция по подключению — кнопка 📖 Инструкция"
            )
        try:
            if telegram_id > 0:
                await bot.send_message(telegram_id, message_text)
            else:
                # покупка с сайта без привязки Telegram — доступ пользователь
                # увидит в профиле на сайте
                log.info("оплата %s: доступ выдан пользователю сайта (без Telegram)", payment_id)
        except Exception:
            log.warning("не удалось отправить доступ клиенту %s", telegram_id)

        await notify_admin(
            f"💰 Оплата: {fmt_price(row['amount'])} ₽ — {html.escape(plan['name'])}\n"
            f"Клиент: {telegram_id}" + (f" (@{html.escape(row['username'])})" if row.get("username") else "")
        )
        await grant_referral_reward(telegram_id)


# ---------- автопродление подписки (только на 1 месяц) ----------

async def _month_plan() -> dict:
    """Тариф «месяц» для автопродления (по id month, иначе первый из PLANS)."""
    plan = await get_plan("month") or ALL_PLANS_BY_ID.get("month")
    if not plan and PLANS:
        plan = PLANS[0]
    if not plan:
        raise WdttError("нет тарифа на 1 месяц для автопродления")
    return plan


async def _autorenew_tick() -> None:
    """Один проход автопродления: уведомления за N часов + списание в момент окончания.

    Защита от повторного списания: попытка фиксируется в БД по сроку подписки
    (renew_charge_exp). Если списание за этот срок уже создано — повторяем
    только его статус, но НЕ создаём новый платёж. Если попытка провалилась
    без создания платежа — следующая попытка не раньше чем через час.
    """
    month = await _month_plan()
    price = float(month["price"])
    now = int(time.time())
    for c in await list_autorenew_customers():
        try:
            await _autorenew_one(c, month, price, now)
        except Exception as e:
            # один клиент не должен ломать цикл для остальных
            log.exception("autorenew: сбой обработки tg %s: %s", c.get("telegram_id"), e)


async def _autorenew_one(c: dict, month: dict, price: float, now: int) -> None:
    """Обработка одного клиента на автопродлении."""
    tg = int(c["telegram_id"])
    exp = int(c.get("expires_at") or 0)
    if exp <= 0:
        return
    left = exp - now
    # льготный период после окончания — 3 дня, дальше не списываем
    if left < -3 * 86400:
        await clear_renew_notified(tg)
        await clear_renew_charge(tg)
        return
    notify_hours = int(c.get("notify_hours") or 0)
    # уведомление о скором списании (один раз за срок)
    if notify_hours > 0 and not c.get("renew_notified") and 0 < left <= notify_hours * 3600:
        try:
            await bot.send_message(
                tg,
                f"⏰ <b>Автопродление подписки</b>\n\nЧерез {notify_hours} ч. (до {fmt_date(exp)}) "
                f"автоматически спишем <b>{fmt_price(price)} ₽</b> с сохранённой карты и продлим подписку "
                f"на <b>30 дней</b> (тариф «{html.escape(month['name'])}»).\n"
                "Отключить автопродление или изменить уведомление можно в настройках мини-приложения или на сайте.",
            )
        except Exception as e:
            log.warning("autorenew: не удалось уведомить tg %s: %s", tg, e)
        await mark_renew_notified(tg)
    # списание: за 5 минут до окончания и в течение льготных 3 дней после
    if not (-3 * 86400 <= left <= 300):
        return

    # --- защита от повторного списания за один и тот же срок ---
    state = await get_renew_charge(tg)
    charged_exp = int((state or {}).get("renew_charge_exp") or 0)
    last_attempt = int((state or {}).get("renew_charge_at") or 0)
    existing_payment = (state or {}).get("renew_charge_payment_id")
    if charged_exp == exp and existing_payment:
        # платёж за этот срок уже создан — НЕ создаём новый, только проверяем статус
        payment_id = str(existing_payment)
        pay = {"id": payment_id, "status": "unknown"}
        log.info("autorenew: tg %s — платёж %s за срок %s уже создан, проверяем статус",
                 tg, payment_id, exp)
    elif charged_exp == exp and last_attempt and (now - last_attempt) < 3600:
        # неудачная попытка без созданного платежа — не чаще раза в час
        return
    else:
        try:
            pay = await charge_saved_method(
                tg, price,
                f"{config.BRAND_NAME} — автопродление на 30 дней",
                plan_id="month",
                idempotency_key=f"autorenew-{tg}-{exp}",
            )
        except Exception as e:
            # попытка без созданного платежа — фиксируем и ждём час до следующей
            await mark_renew_charge(tg, exp, None)
            log.warning("autorenew: списание не удалось для tg %s: %s", tg, e)
            try:
                await bot.send_message(
                    tg,
                    f"⚠️ <b>Автопродление не удалось</b>\n\nНе получилось списать {fmt_price(price)} ₽ "
                    f"с сохранённой карты ({html.escape(str(e)[:200])}).\n"
                    "Продлите подписку вручную или привяжите другую карту. Подписка действует до "
                    f"{fmt_date(exp)}.",
                )
            except Exception:
                pass
            return
        payment_id = pay["id"]
        # сразу фиксируем факт списания — чтобы следующий тик не создал второй платёж
        await mark_renew_charge(tg, exp, payment_id)
        await add_payment(telegram_id=tg, username=c.get("username"),
                          payment_id=payment_id, plan_id="month",
                          amount=f"{price:.2f}")

    # статус платежа (сеть может моргнуть — это не должно ломать цикл)
    yk = None
    try:
        yk = await fetch_payment(payment_id)
    except Exception as e:
        log.warning("autorenew: не удалось получить статус платежа %s: %s", payment_id, e)
    status = (yk or {}).get("status") or pay.get("status") or "pending"
    if status == "succeeded":
        await set_payment_status(payment_id, "fulfilled", paid_at=(yk or {}).get("paid_at"))
        try:
            await grant_access(tg, c.get("username"), month)
        except Exception as e:
            log.exception("autorenew: grant_access для tg %s", tg)
            await notify_admin(f"🚨 Автопродление: оплата прошла, но выдача не удалась для {tg}: {e}")
            return
        await clear_renew_notified(tg)
        customer = await get_customer(tg)
        new_exp = int((customer or {}).get("expires_at") or 0)
        try:
            await bot.send_message(
                tg,
                f"✅ <b>Подписка автоматически продлена на 30 дней</b>\n\n"
                f"Списано {fmt_price(price)} ₽ с сохранённой карты. Действует до {fmt_date(new_exp)}.",
            )
        except Exception:
            pass
        await notify_admin(
            f"🔄 Автопродление: {fmt_price(price)} ₽ — клиент {tg}"
            + (f" (@{html.escape(c['username'])})" if c.get("username") else "")
            + f", продлено до {fmt_date(new_exp)}"
        )
    elif status == "pending":
        # требуется подтверждение (3DS) — отправим ссылку пользователю
        conf = (yk or {}).get("confirmation_url")
        try:
            await bot.send_message(
                tg,
                f"🔐 Для автопродления нужно подтверждение платежа: {fmt_price(price)} ₽.\n"
                + (f"Откройте ссылку и подтвердите: {conf}" if conf else "Попробуйте продлить вручную."),
            )
        except Exception:
            pass
    else:
        try:
            await bot.send_message(
                tg,
                f"⚠️ Автопродление не прошло (статус: {status}). Подписка действует до {fmt_date(exp)} — "
                "продлите вручную.",
            )
        except Exception:
            pass


async def autorenew_worker() -> None:
    """Фоновый цикл автопродления: каждые 30 минут проверяет подписки с auto_renew=1."""
    while True:
        try:
            await asyncio.sleep(1800)
            await _autorenew_tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("autorenew_worker: %s", e)
            await asyncio.sleep(300)


# ---------- админ: кнопки ----------

@router.message(F.text == "📊 Статистика", StateFilter(None))
async def nav_admin_stats(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    s = await stats()
    servers = await list_servers()
    await message.answer(
        "🛠 <b>Админ-панель</b>\n\n"
        f"Всего клиентов: {s['customers']}\n"
        f"Активных: {s['active']}\n"
        f"Серверов WDTT: {len(servers)}\n"
        f"Оплаченных платежей: {s['paid']}\n"
        f"Выручка: {fmt_price(s['revenue'])} ₽"
    )


@router.message(F.text == "👥 Клиенты", StateFilter(None))
async def nav_users(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    customers = await get_active_customers()
    if not customers:
        await message.answer("Активных клиентов нет.")
        return
    lines = ["👥 <b>Активные клиенты</b>:"]
    for c in customers[:100]:
        username = f" @{c['username']}" if c.get("username") else ""
        lines.append(
            f"• {c['telegram_id']}{username} — до {fmt_date(c['expires_at'])} "
            f"({c.get('plan_id') or '—'}, сервер #{c.get('server_id') or '?'})"
        )
    if len(customers) > 100:
        lines.append(f"… и ещё {len(customers) - 100}")
    await message.answer("\n".join(lines))


@router.message(F.text == "🖥 Серверы", StateFilter(None))
async def nav_servers(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    servers = await list_servers()
    if not servers:
        await message.answer("Серверы не настроены. Добавьте: кнопка ➕ Добавить сервер.")
        return
    counts = await server_active_counts()
    lines = ["🖥 <b>Серверы WDTT</b>:"]
    for s in servers:
        panel_info = ""
        try:
            inbound = await (await get_client(s["id"])).get_inbound()
            panel_info = (
                f" · панель: {inbound.get('active_users', '?')} акт./"
                f"{inbound.get('online_users', '?')} онлайн, лимит {inbound.get('max_users', '?')}"
            )
        except Exception as e:
            panel_info = f" · панель недоступна ({e})"
        lines.append(
            f"#{s['id']} <b>{html.escape(s['name'])}</b> — {html.escape(s['base_url'])}\n"
            f"   клиентов (бот): {counts.get(s['id'], 0)}{panel_info}"
        )
    await message.answer("\n".join(lines))


@router.message(F.text == "⛔ Отозвать", StateFilter(None))
async def nav_revoke(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.revoke_id)
    await message.answer("Отправьте Telegram ID клиента, у которого отозвать доступ.")


@router.message(StateFilter(AdminStates.revoke_id))
async def process_revoke(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Нужен числовой Telegram ID. Попробуйте ещё раз.")
        return
    telegram_id = int(raw)
    await state.clear()
    customer = await get_customer(telegram_id)
    if not customer or not customer["wdtt_password"]:
        await message.answer("У клиента нет активного ключа.")
        return
    try:
        client = await get_client(customer["server_id"])
        await client.delete_user(customer["wdtt_password"])
    except Exception as e:
        await message.answer(f"⚠️ Не удалось удалить ключ из панели: {e}")
        return
    await clear_customer(telegram_id)
    try:
        await bot.send_message(telegram_id, "⛔️ Ваш доступ к WDTT отозван администратором.")
    except Exception:
        pass
    await message.answer(f"✅ Доступ клиента {telegram_id} отозван.")


@router.message(F.text == "➕ Продлить", StateFilter(None))
async def nav_extend(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.extend_data)
    await message.answer("Отправьте ID клиента и число дней через пробел, например: 123456789 30")


@router.message(StateFilter(AdminStates.extend_data))
async def process_extend(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        await message.answer("Нужно два числа через пробел: <telegram_id> <дней>. Попробуйте ещё раз.")
        return
    telegram_id, days = int(parts[0]), int(parts[1])
    await state.clear()
    customer = await get_customer(telegram_id)
    if not customer or not customer["wdtt_password"]:
        await message.answer("У клиента нет активного ключа (пусть купит подписку или пробный период).")
        return
    try:
        client = await get_client(customer["server_id"])
        info = await client.get_user_info(customer["wdtt_password"]) or {}
        now = int(time.time())
        expires = max(now, int(customer["expires_at"] or 0)) + days * 86400
        vk_hash = WdttClient._clean_hash_pieces(str(info.get("vk_hash") or ""))
        await client.update_user(
            old_password=customer["wdtt_password"], expires_at=expires,
            total_gb=int(info.get("total_gb") or 0),
            max_devices=int(info.get("max_devices") or 1),
            vk_hash=",".join(vk_hash),  # иначе панель обнулит hash
        )
        await upsert_customer(telegram_id, customer["wdtt_password"], expires,
                              customer["plan_id"], customer["server_id"],
                              username=customer["username"])
    except Exception as e:
        await message.answer(f"⚠️ Ошибка продления: {e}")
        return
    try:
        await bot.send_message(telegram_id, f"🎁 Администратор продлил вашу подписку на {days} дн. (до {fmt_date(expires)}).")
    except Exception:
        pass
    await message.answer(f"✅ Подписка клиента {telegram_id} продлена на {days} дн. (до {fmt_date(expires)}).")


@router.message(F.text == "🎁 Подарить", StateFilter(None))
async def nav_gift(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.gift_id)
    await message.answer("Кому подарить подписку? Отправьте Telegram ID клиента.")


@router.message(StateFilter(AdminStates.gift_id))
async def process_gift_id(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Нужен числовой Telegram ID. Попробуйте ещё раз.")
        return
    await state.update_data(gift_id=int(text))
    await state.set_state(AdminStates.gift_days)
    await message.answer(f"Срок подарка в днях для tg {text}:")


@router.message(StateFilter(AdminStates.gift_days))
async def process_gift_days(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("Нужно целое число дней (например, 7). Попробуйте ещё раз.")
        return
    days = int(text)
    data = await state.get_data()
    telegram_id = int(data["gift_id"])
    await state.clear()
    try:
        client, pwd, expires = await grant_gift(telegram_id, days)
    except Exception as e:
        await message.answer(f"⚠️ Ошибка выдачи подарка: {e}")
        return
    await message.answer(f"✅ tg {telegram_id}: подарочная подписка на {days} дн. (до {fmt_date(expires)}).")
    try:
        await bot.send_message(telegram_id,
                               f"🎉 <b>Вам подарена подписка WDTT на {days} дн.!</b> "
                               f"Действует до {fmt_date(expires)}.\n"
                               "Ссылки: кнопка 👤 Моя подписка")
    except Exception:
        pass


async def grant_gift(telegram_id: int, days: int) -> tuple[WdttClient, str, int]:
    """Подарочная подписка: безлимитный трафик, устройств как у пробника."""
    plan = {
        "id": "gift",
        "name": "Подарочная подписка",
        "days": int(days),
        "traffic_gb": 0,
        "max_devices": TRIAL["max_devices"],
    }
    return await grant_access(telegram_id, None, plan)


@router.message(F.text == "➕ Добавить сервер", StateFilter(None))
async def nav_add_server(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.add_server_name)
    await message.answer("Шаг 1/4: отправьте имя сервера (например, de2).")


@router.message(StateFilter(AdminStates.add_server_name))
async def process_add_server_name(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("Имя не может быть пустым. Попробуйте ещё раз.")
        return
    await state.update_data(name=name)
    await state.set_state(AdminStates.add_server_url)
    await message.answer("Шаг 2/4: отправьте base_url панели (например, https://5.6.7.8:2860/wdtt).")


@router.message(StateFilter(AdminStates.add_server_url))
async def process_add_server_url(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    base_url = (message.text or "").strip()
    if not base_url.startswith(("http://", "https://")):
        await message.answer("base_url должен начинаться с https:// (или http:// для теста). Попробуйте ещё раз.")
        return
    await state.update_data(base_url=base_url)
    await state.set_state(AdminStates.add_server_login)
    await message.answer("Шаг 3/4: отправьте логин панели WDTT.")


@router.message(StateFilter(AdminStates.add_server_login))
async def process_add_server_login(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    username = (message.text or "").strip()
    if not username:
        await message.answer("Логин не может быть пустым. Попробуйте ещё раз.")
        return
    await state.update_data(username=username)
    await state.set_state(AdminStates.add_server_password)
    await message.answer("Шаг 4/4: отправьте пароль панели WDTT.")


@router.message(StateFilter(AdminStates.add_server_password))
async def process_add_server_password(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    password = (message.text or "").strip()
    if not password:
        await message.answer("Пароль не может быть пустым. Попробуйте ещё раз.")
        return
    data = await state.get_data()
    await state.clear()
    base_url = data["base_url"]
    verify_ssl = base_url.startswith("https://")
    probe = WdttClient(base_url, data["username"], password, verify_ssl=verify_ssl)
    try:
        await probe.login()
    except Exception as e:
        await message.answer(f"⚠️ Не удалось войти в панель: {e}")
        return
    finally:
        await probe.close()
    server_id = await add_server(data["name"], base_url, data["username"], password, verify_ssl)
    await message.answer(
        f"✅ Сервер «{html.escape(data['name'])}» добавлен (#{server_id}). "
        "Новые клиенты будут распределяться на наименее загруженный сервер."
    )


@router.message(F.text == "🗑 Удалить сервер", StateFilter(None))
async def nav_del_server(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.del_server_id)
    await message.answer("Отправьте ID сервера, который удалить.")


@router.message(StateFilter(AdminStates.del_server_id))
async def process_del_server(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Нужен числовой ID сервера. Попробуйте ещё раз.")
        return
    await state.clear()
    server_id = int(raw)
    counts = await server_active_counts()
    if counts.get(server_id, 0) > 0:
        await message.answer(
            f"⚠️ На сервере #{server_id} ещё {counts[server_id]} активных клиентов — сначала отзовите их (кнопка ⛔ Отозвать)."
        )
        return
    if not await get_server(server_id):
        await message.answer(f"Сервер #{server_id} не найден.")
        return
    client = clients.pop(server_id, None)
    if client:
        await client.close()
    await delete_server(server_id)
    await message.answer(f"✅ Сервер #{server_id} удалён.")


# ---------- админ: автоустановка сервера по SSH ----------

@router.message(F.text == "🚀 Установить сервер", StateFilter(None))
async def nav_install_server(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.install_name)
    await message.answer(
        "🚀 Автоустановка панели WDTT на новый сервер.\n"
        "Шаг 1/5: отправьте имя сервера (например, de2)."
    )


@router.message(StateFilter(AdminStates.install_name))
async def process_install_name(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("Имя не может быть пустым. Попробуйте ещё раз.")
        return
    await state.update_data(name=name)
    await state.set_state(AdminStates.install_host)
    await message.answer("Шаг 2/5: отправьте IP-адрес сервера (у арендодателя).")


@router.message(StateFilter(AdminStates.install_host))
async def process_install_host(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    host = (message.text or "").strip()
    if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$|^([0-9a-fA-F:]+)$|^[a-z0-9.-]+$", host):
        await message.answer("Похоже, это не IP/домен. Отправьте IP-адрес сервера ещё раз.")
        return
    await state.update_data(host=host)
    await state.set_state(AdminStates.install_ssh_port)
    await message.answer("Шаг 3/5: порт SSH (обычно 22).")


@router.message(StateFilter(AdminStates.install_ssh_port))
async def process_install_ssh_port(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 65535):
        await message.answer("Нужен номер порта (1-65535), например 22. Попробуйте ещё раз.")
        return
    await state.update_data(ssh_port=int(raw))
    await state.set_state(AdminStates.install_ssh_user)
    await message.answer("Шаг 4/5: логин SSH у арендодателя (обычно root).")


@router.message(StateFilter(AdminStates.install_ssh_user))
async def process_install_ssh_user(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    user = (message.text or "").strip()
    if not user:
        await message.answer("Логин не может быть пустым. Попробуйте ещё раз.")
        return
    await state.update_data(ssh_user=user)
    await state.set_state(AdminStates.install_ssh_password)
    await message.answer("Шаг 5/5: пароль SSH у арендодателя.")


@router.message(StateFilter(AdminStates.install_ssh_password))
async def process_install_ssh_password(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    ssh_password = (message.text or "").strip()
    if not ssh_password:
        await message.answer("Пароль не может быть пустым. Попробуйте ещё раз.")
        return
    data = await state.get_data()
    await state.clear()

    host = data["host"]
    await message.answer(
        f"⏳ Устанавливаю панель WDTT на <b>{html.escape(host)}</b> "
        f"(сервер «{html.escape(data['name'])}»)... Это занимает 2-10 минут."
    )
    try:
        # cookies самого свежего VK-аккаунта загрузим в панель сразу
        accounts = await list_vk_accounts()
        vk_cookies = accounts[0]["cookies"] if accounts else None
        result = await deploy_server(
            host=host,
            ssh_port=data["ssh_port"],
            ssh_user=data["ssh_user"],
            ssh_password=ssh_password,
            name=data["name"],
            vk_cookies=vk_cookies,
        )
    except SetupError as e:
        log.warning("установка сервера %s не удалась: %s", host, e)
        await message.answer(f"⚠️ Не удалось установить панель на {html.escape(host)}:\n<code>{html.escape(str(e)[:1500])}</code>")
        return
    except Exception as e:
        log.exception("установка сервера %s", host)
        await message.answer(f"⚠️ Ошибка установки на {html.escape(host)}: <code>{html.escape(str(e)[:1500])}</code>")
        return

    vk_note = ""
    if result.get("cookies_ok") is False:
        vk_note = "\n⚠️ VK cookies не приняты панелью — добавьте VK-аккаунт (➕ VK аккаунт) и повторите."
    elif not vk_cookies:
        vk_note = "\nℹ️ VK-аккаунты не заданы — хеши создаваться не будут, пока не добавите (➕ VK аккаунт)."
    await message.answer(
        f"✅ Сервер «{html.escape(result['name'])}» установлен и добавлен как <b>#{result['id']}</b>!\n\n"
        f"🌐 Панель: <code>{html.escape(result['base_url'])}</code>\n"
        f"👤 Логин панели: <code>{html.escape(result['panel_username'])}</code>\n"
        f"🔑 Пароль панели: <code>{html.escape(result['panel_password'])}</code>\n"
        f"👥 Лимит пользователей: {result['max_users']}\n"
        f"🔑 VK cookies загружены: {'да' if result.get('cookies_ok') else 'нет/не проверено'}"
        f"{vk_note}\n\n"
        "Новые клиенты автоматически распределяются на наименее загруженный сервер "
        f"(лимит {config.SERVER_MAX_USERS} на панель)."
    )


# ---------- админ: VK-аккаунты ----------

@router.message(F.text == "🔑 VK аккаунты", StateFilter(None))
async def nav_vk_accounts(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    accounts = await list_vk_accounts(active_only=False)
    if not accounts:
        await message.answer(
            "VK-аккаунтов нет. Добавьте: ➕ VK аккаунт (отправьте название, затем cookies — "
            "JSON cookies-vk.json или строку с remixsid=...).\n"
            "Все аккаунты будут по очереди загружаться в каждую панель, а VK-хеши браться "
            "от профиля с наименьшим числом выданных хешей."
        )
        return
    counts_all = await vk_issued_counts(None)
    servers = await list_servers()
    lines = ["🔑 <b>VK-аккаунты</b> (cookies профилей для создания хешей):"]
    for a in accounts:
        status = "активен" if a.get("active") else "отключён"
        lines.append(
            f"#{a['id']} <b>{html.escape(a['label'])}</b> — {status}, "
            f"выдано хешей: {counts_all.get(a['id'], 0)}"
        )
    if servers:
        lines.append("\nПо серверам:")
        for s in servers:
            counts_srv = await vk_issued_counts(s["id"])
            per = ", ".join(f"#{aid}:{c}" for aid, c in sorted(counts_srv.items())) or "—"
            lines.append(f"#{s['id']} {html.escape(s['name'])}: {per}")
    lines.append("\n➕ VK аккаунт — добавить, 🗑 VK аккаунт — отключить по ID.")
    await message.answer("\n".join(lines))


@router.message(F.text == "➕ VK аккаунт", StateFilter(None))
async def nav_add_vk_account(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.vk_add_label)
    await message.answer(
        "Шаг 1/2: отправьте название VK-аккаунта (например, vk1 / Основной).\n"
        "Шаг 2/2: отправьте cookies этого профиля — JSON-массив cookies-vk.json "
        "(из WhitelistBypass.Creator) или строку вида <code>remixsid=...; ...</code>."
    )


@router.message(StateFilter(AdminStates.vk_add_label))
async def process_vk_add_label(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    label = (message.text or "").strip()
    if not label:
        await message.answer("Название не может быть пустым. Попробуйте ещё раз.")
        return
    await state.update_data(label=label)
    await state.set_state(AdminStates.vk_add_cookies)
    await message.answer("Шаг 2/2: отправьте cookies профиля (JSON или строка с remixsid).")


@router.message(StateFilter(AdminStates.vk_add_cookies))
async def process_vk_add_cookies(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    cookies = (message.text or "").strip()
    if "remixsid" not in cookies.lower():
        await message.answer(
            "⚠️ В cookies нет remixsid — такой профиль не подходит. "
            "Экспортируйте cookies через WhitelistBypass.Creator (или DevTools: значение remixsid). "
            "Отправьте cookies ещё раз."
        )
        return
    data = await state.get_data()
    await state.clear()
    account_id = await add_vk_account(data["label"], cookies)
    await message.answer(
        f"✅ VK-аккаунт <b>{html.escape(data['label'])}</b> добавлен (#{account_id}).\n"
        "При создании VK-хешей бот будет по очереди использовать все аккаунты, "
        "начиная с того, у кого меньше всего выдано хешей."
    )


@router.message(F.text == "🗑 VK аккаунт", StateFilter(None))
async def nav_del_vk_account(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.vk_del_id)
    await message.answer("Отправьте ID VK-аккаунта, который отключить (список: 🔑 VK аккаунты).")


@router.message(StateFilter(AdminStates.vk_del_id))
async def process_del_vk_account(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Нужен числовой ID аккаунта. Попробуйте ещё раз.")
        return
    account_id = int(raw)
    await state.clear()
    account = await get_vk_account(account_id)
    if not account:
        await message.answer(f"VK-аккаунт #{account_id} не найден.")
        return
    await delete_vk_account(account_id)
    await message.answer(f"✅ VK-аккаунт #{account_id} ({html.escape(account['label'])}) отключён.")


# ---------- админ: ссылки всех пользователей сервера ----------

@router.message(F.text == "🔗 Ссылки серверов", StateFilter(None))
async def nav_server_links(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    servers = await list_servers()
    if not servers:
        await message.answer("Серверов нет.")
        return
    await state.set_state(AdminStates.server_links_id)
    await message.answer(
        "Отправьте ID сервера, все ссылки wdtt:// которого показать "
        "(список серверов — кнопка 🖥 Серверы):\n"
        + "\n".join(f"#{s['id']} — {s['name']} ({s['base_url']})" for s in servers)
    )


@router.message(StateFilter(AdminStates.server_links_id))
async def process_server_links(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Нужен числовой ID сервера. Попробуйте ещё раз.")
        return
    server_id = int(raw)
    await state.clear()
    server = await get_server(server_id)
    if not server:
        await message.answer(f"Сервер #{server_id} не найден.")
        return
    try:
        client = await get_client(server_id)
        data = await client.get_users()
    except Exception as e:
        await message.answer(f"⚠️ Панель сервера #{server_id} недоступна: {e}")
        return
    users = data.get("users") or []
    if not users:
        await message.answer(f"На сервере #{server_id} ({html.escape(server['name'])}) пользователей нет.")
        return
    await message.answer(
        f"🔗 Сервер #{server_id} <b>{html.escape(server['name'])}</b> — "
        f"пользователей: {len(users)}. Ссылки ниже (по 20 шт. в сообщении):"
    )
    # chunking по 4000 символов (лимит Telegram)
    chunk: list[str] = []
    chunk_len = 0
    sent = 0
    for u in users:
        pwd = u.get("password_key") or u.get("password") or "?"
        comment = html.escape(str(u.get("comment") or "—"))
        link = html.escape(str(u.get("link") or ""))
        line = f"• {comment} (<code>{pwd}</code>)\n<code>{link}</code>"
        if chunk_len + len(line) > 3800 and chunk:
            await message.answer("\n\n".join(chunk))
            sent += 1
            chunk, chunk_len = [], 0
        chunk.append(line)
        chunk_len += len(line)
    if chunk:
        await message.answer("\n\n".join(chunk))
        sent += 1
    await message.answer(f"✅ Отправлено сообщений со ссылками: {sent} (всего пользователей: {len(users)}).")


# ---------- админ: добить VK-хеши до 4 ----------

@router.message(F.text == "🔄 VK хеши добить", StateFilter(None))
async def nav_backfill_vk(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.answer("⏳ Проверяю активных клиентов и добиваю VK-хеши до 4...")
    try:
        result = await backfill_vk_hashes()
    except Exception as e:
        log.exception("backfill_vk_hashes")
        await message.answer(f"⚠️ Ошибка: {e}")
        return
    await message.answer(
        "✅ Готово!\n"
        f"Проверено клиентов: {result['checked']}\n"
        f"Добито хешей (было < 4): {result['topped']}\n"
        f"Ошибок: {result['failed']}"
    )


# ---------- админ: тарифы ----------

def fmt_plan_short(p: dict) -> str:
    return (
        f"• <code>{html.escape(str(p['id']))}</code> {html.escape(str(p['name']))} — "
        f"{fmt_price(p['price'])} ₽ · {p['days']} дн. · "
        f"{fmt_traffic(p.get('traffic_gb'))} · {p.get('max_devices', 1)} устр."
    )


@router.message(F.text == "🗂 Тарифы", StateFilter(None))
async def nav_plans(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    plans = await list_plans()
    if not plans:
        await message.answer("Тарифов нет. Добавьте: кнопка ➕ Добавить тариф.")
        return
    lines = ["🗂 <b>Тарифы</b>:"]
    lines += [fmt_plan_short(p) for p in plans]
    lines.append("\nID используется в управлении тарифами и показывается клиентам в оплате. Изменить: ✏️, удалить: 🗑.")
    await message.answer("\n".join(lines))


@router.message(F.text == "➕ Добавить тариф", StateFilter(None))
async def nav_add_plan(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.plan_add_id)
    await message.answer("Шаг 1/6: отправьте ID тарифа латиницей (например, week). ID добавить и изменить потом нельзя.")


@router.message(StateFilter(AdminStates.plan_add_id))
async def process_add_plan_id(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    plan_id = (message.text or "").strip()
    if not plan_id or not all(c.isalnum() or c in "-_" for c in plan_id):
        await message.answer("ID может содержать только латиницу, цифры, - и _. Попробуйте ещё раз.")
        return
    if plan_id == "trial":
        await message.answer("ID «trial» занят пробным периодом. Выберите другой.")
        return
    if await get_plan(plan_id):
        await message.answer(f"Тариф с ID «{plan_id}» уже существует. Изменить его можно кнопкой ✏️ Изменить тариф.")
        return
    await state.update_data(plan_id=plan_id)
    await state.set_state(AdminStates.plan_add_name)
    await message.answer("Шаг 2/6: отправьте название тарифа (например, Месяц).")


@router.message(StateFilter(AdminStates.plan_add_name))
async def process_add_plan_name(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым. Попробуйте ещё раз.")
        return
    await state.update_data(name=name)
    await state.set_state(AdminStates.plan_add_days)
    await message.answer("Шаг 3/6: отправьте срок в днях (например, 30).")


@router.message(StateFilter(AdminStates.plan_add_days))
async def process_add_plan_days(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("Нужно целое число больше 0. Попробуйте ещё раз.")
        return
    await state.update_data(days=int(raw))
    await state.set_state(AdminStates.plan_add_price)
    await message.answer("Шаг 4/6: отправьте цену в рублях (например, 249; 0 — бесплатный тариф).")


@router.message(StateFilter(AdminStates.plan_add_price))
async def process_add_plan_price(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    try:
        price = float((message.text or "").strip().replace(",", "."))
    except ValueError:
        await message.answer("Нужно число, например 249 или 249.50. Попробуйте ещё раз.")
        return
    if price < 0:
        await message.answer("Цена не может быть отрицательной. Попробуйте ещё раз.")
        return
    await state.update_data(price=price)
    await state.set_state(AdminStates.plan_add_traffic)
    await message.answer("Шаг 5/6: отправьте лимит трафика в ГБ (0 — безлимит).")


@router.message(StateFilter(AdminStates.plan_add_traffic))
async def process_add_plan_traffic(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) < 0:
        await message.answer("Нужно целое число 0 или больше (0 — безлимит). Попробуйте ещё раз.")
        return
    await state.update_data(traffic_gb=int(raw))
    await state.set_state(AdminStates.plan_add_devices)
    await message.answer("Шаг 6/6: отправьте число устройств (например, 2).")


@router.message(StateFilter(AdminStates.plan_add_devices))
async def process_add_plan_devices(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("Нужно целое число больше 0. Попробуйте ещё раз.")
        return
    data = await state.get_data()
    await state.clear()
    await upsert_plan(data["plan_id"], data["name"], data["days"],
                      data["price"], data["traffic_gb"], int(raw))
    await message.answer(
        "✅ Тариф добавлен:\n" + fmt_plan_short(await get_plan(data["plan_id"]))
    )


@router.message(F.text == "✏️ Изменить тариф", StateFilter(None))
async def nav_edit_plan(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.plan_edit_id)
    await message.answer("Отправьте ID тарифа, который изменить (список: кнопка 🗂 Тарифы).")


@router.message(StateFilter(AdminStates.plan_edit_id))
async def process_edit_plan_id(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    plan_id = (message.text or "").strip()
    plan = await get_plan(plan_id)
    if not plan:
        await message.answer("Тариф не найден. Проверьте ID (список: кнопка 🗂 Тарифы) и попробуйте ещё раз.")
        return
    await state.update_data(plan_id=plan_id)
    await state.set_state(AdminStates.plan_edit_values)
    await message.answer(
        "Текущие значения:\n" + fmt_plan_short(plan) + "\n\n"
        "Отправьте новые значения одной строкой: <дней> <цена ₽> <ГБ> <устройств>\n"
        "Пример: 30 249 100 2 (0 ГБ = безлимит, 0 ₽ = бесплатный тариф).\n"
        "Название и ID не меняются."
    )


@router.message(StateFilter(AdminStates.plan_edit_values))
async def process_edit_plan_values(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 4:
        await message.answer("Нужно 4 значения через пробел: <дней> <цена> <ГБ> <устройств>. Попробуйте ещё раз.")
        return
    if not parts[0].isdigit() or not parts[2].isdigit() or not parts[3].isdigit():
        await message.answer("Дней, ГБ и устройств — целые числа. Цена может быть дробной (например, 249.50). Попробуйте ещё раз.")
        return
    try:
        price = float(parts[1])
    except ValueError:
        await message.answer("Цена должна быть числом, например 249 или 249.50. Попробуйте ещё раз.")
        return
    if price < 0:
        await message.answer("Цена не может быть отрицательной. Попробуйте ещё раз.")
        return
    days, traffic_gb, devices = int(parts[0]), int(parts[2]), int(parts[3])
    if days <= 0 or devices <= 0 or traffic_gb < 0:
        await message.answer("Дней и устройств — больше 0, трафик — 0 или больше (0 = безлимит). Попробуйте ещё раз.")
        return
    plan_id = (await state.get_data())["plan_id"]
    plan = await get_plan(plan_id)
    if not plan:
        await state.clear()
        await message.answer("Тариф не найден.")
        return
    await state.clear()
    await upsert_plan(plan_id, plan["name"], days, price, traffic_gb, devices)
    await message.answer("✅ Тариф обновлён:\n" + fmt_plan_short(await get_plan(plan_id)))


@router.message(F.text == "🗑 Удалить тариф", StateFilter(None))
async def nav_del_plan(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.plan_del_id)
    await message.answer("Отправьте ID тарифа, который удалить (список: кнопка 🗂 Тарифы).")


@router.message(StateFilter(AdminStates.plan_del_id))
async def process_del_plan_id(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    plan_id = (message.text or "").strip()
    plan = await get_plan(plan_id)
    if not plan:
        await message.answer("Тариф не найден. Проверьте ID (список: кнопка 🗂 Тарифы) и попробуйте ещё раз.")
        return
    await state.clear()
    await delete_plan(plan_id)
    await message.answer(f"✅ Тариф «{html.escape(str(plan['name']))}» ({plan_id}) удалён.")


# ---------- фоновые задачи ----------

async def poller() -> None:
    """Периодически проверяет незавершённые платежи (работает без webhook)."""
    while True:
        try:
            for p in await get_pending_payments():
                await fulfill_payment(p["payment_id"])
            await refresh_user_metrics()
        except Exception:
            log.exception("poller loop")
        await asyncio.sleep(config.POLL_INTERVAL)


async def send_expiry_reminders() -> int:
    """Разовые напоминания за 3/2/1 день до окончания. Возвращает число отправленных."""
    REMINDER_DAYS = (3, 2, 1)
    sent = 0
    now = int(time.time())
    for c in await get_active_customers():
        exp = int(c["expires_at"] or 0)
        if not c["wdtt_password"] or exp <= now:
            continue
        days_left = -((now - exp) // 86400)  # ceil((exp - now) / 86400)
        # ближайший порог напоминания: 3 -> 2 -> 1 день
        for d in REMINDER_DAYS:
            if days_left > d:
                continue
            if await claim_reminder(c["telegram_id"], d):
                tail = "завтра" if days_left <= 1 else f"через {days_left} дн."
                try:
                    await bot.send_message(
                        c["telegram_id"],
                        f"⏳ {tail} ({fmt_date(exp)}) истекает ваша подписка на WDTT. "
                        "Продлить: кнопка 🛒 Купить",
                    )
                    sent += 1
                except Exception:
                    pass
            break
    return sent


TRAFFIC_NOTICE_REMAINING = 150 * 1024**2  # предупреждать, когда осталось <= 150 МБ


async def send_traffic_warnings() -> int:
    """Предупреждение, когда остаток лимита трафика упал до 150 МБ. Возвращает число отправленных."""
    sent = 0
    for c in await get_active_customers():
        if not c["wdtt_password"]:
            continue
        plan = await get_plan(c["plan_id"]) or ALL_PLANS_BY_ID.get(c["plan_id"])
        limit_gb = (plan or {}).get("traffic_gb") or 0
        if not limit_gb:
            continue
        try:
            client = await get_client(c["server_id"])
            info = await client.get_user_info(c["wdtt_password"])
            if not info:
                continue
            used = int(info.get("up_bytes") or 0) + int(info.get("down_bytes") or 0)
            remaining = int(float(limit_gb) * 1024**3) - used
        except Exception:
            continue
        if remaining > TRAFFIC_NOTICE_REMAINING:
            continue
        if await claim_traffic_notice(c["telegram_id"]):
            try:
                await bot.send_message(
                    c["telegram_id"],
                    f"📉 Осталось {fmt_bytes(remaining)} из {fmt_traffic(limit_gb)} — трафик скоро закончится. "
                    "Продлить: кнопка 🛒 Купить",
                )
                sent += 1
            except Exception:
                pass
    return sent


# Льготный период после окончания подписки для клиентов с автопродлением:
# ключ нельзя удалять сразу, иначе воркер автопродления не сможет списать
# деньги и продлить подписку (он ищет customers.wdtt_password IS NOT NULL).
AUTORENEW_GRACE_SECONDS = 3 * 86400


def _expired_key_cleanup_due(c: dict, now: int) -> bool:
    """Можно ли уже удалять ключ истёкшего клиента.

    Клиентам с автопродлением и сохранённой картой даём льготный период:
    иначе ключ удаляется раньше, чем autorenew_worker успеет списать деньги.
    """
    exp = int(c.get("expires_at") or 0)
    if c.get("auto_renew") and c.get("payment_method_id"):
        if now - exp < AUTORENEW_GRACE_SECONDS:
            return False
    return True


async def expiry_worker() -> None:
    """Раз в час отправляет напоминания и удаляет истёкшие ключи."""
    while True:
        try:
            await send_expiry_reminders()
            await send_traffic_warnings()
            now = int(time.time())
            for c in await get_expired_customers():
                if not _expired_key_cleanup_due(c, now):
                    # даём autorenew_worker шанс списать и продлить
                    continue
                cleared = False
                try:
                    client = await get_client(c["server_id"])
                    await client.delete_user(c["wdtt_password"])
                    cleared = True
                except Exception:
                    log.exception("не удалось удалить истёкший ключ tg %s", c["telegram_id"])
                if cleared:
                    await clear_customer(c["telegram_id"])
                    try:
                        await bot.send_message(
                            c["telegram_id"],
                            "⏰ Ваша подписка на WDTT истекла. Продлить: кнопка 🛒 Купить",
                        )
                    except Exception:
                        pass
        except Exception:
            log.exception("expiry loop")
        await asyncio.sleep(3600)


def _panel_expiry_changed(local_expires: int, panel_expires: int) -> bool:
    """Нужно ли обновлять локальную запись по данным панели.

    customers не хранит total_gb/max_devices/vk_hash — эти поля живут только
    в панели, сравнивать их не с чем. Единственное синхронизируемое поле —
    expires_at (админ мог продлить/сократить срок вручную).
    """
    return bool(panel_expires) and int(panel_expires) != int(local_expires)


async def sync_panel_worker() -> None:
    """Периодически синхронизирует пользователей из панели в локальную БД.

    Полезно, если админ менял срок/лимит/устройства/хэш прямо в панели.
    """
    while True:
        try:
            servers = await list_servers()
            if not servers:
                await asyncio.sleep(3600)
                continue

            for server in servers:
                try:
                    client = await get_client(server["id"])
                    panel_users = await client.get_users()
                    users_list = panel_users.get("users") or []
                except Exception as e:
                    log.warning("sync panel: сервер %s недоступен: %s", server["id"], e)
                    continue

                for u in users_list:
                    pwd = u.get("password_key") or u.get("password")
                    if not pwd:
                        continue
                    # быстрый поиск по индексу вместо полного перебора
                    cust = await get_customer_by_password(pwd)
                    if not cust:
                        continue  # пользователь создан не через бота — игнорируем

                    panel_expires = int(u.get("expires_at") or 0)
                    local_expires = int(cust.get("expires_at") or 0)
                    if not _panel_expiry_changed(local_expires, panel_expires):
                        continue

                    await upsert_customer(
                        cust["telegram_id"],
                        pwd,
                        panel_expires,
                        cust.get("plan_id"),
                        cust.get("server_id"),
                        username=cust.get("username"),
                    )
                    log.info("sync panel: tg %s обновлён из панели (exp=%s)",
                             cust["telegram_id"], panel_expires)

        except Exception:
            log.exception("sync panel loop")
        await asyncio.sleep(3600)


async def on_webhook_payment(payment_id: str) -> None:
    try:
        await fulfill_payment(payment_id)
    except Exception:
        log.exception("webhook fulfill")


async def run_webhook() -> None:
    from aiohttp import web
    from webhook import make_webhook_app

    app = make_webhook_app(
        config.WEBHOOK_PATH,
        on_webhook_payment,
        check_ip=not config.WEBHOOK_SKIP_IP_CHECK,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.WEBHOOK_HOST, config.WEBHOOK_PORT)
    await site.start()
    log.info("Webhook ЮKassa слушает на http://%s:%s%s (IP-check: %s)",
             config.WEBHOOK_HOST, config.WEBHOOK_PORT, config.WEBHOOK_PATH,
             "off" if config.WEBHOOK_SKIP_IP_CHECK else "on")


def _metrics_allowed(remote: str | None, authorization: str | None, token: str) -> bool:
    """Доступ к /metrics: токен, а без него — только с loopback.

    Метрики содержат число клиентов/платежей и латентность панели — не стоит
    отдавать их всему интернету на публичном HEALTH_PORT.
    """
    if token:
        return (authorization or "") == f"Bearer {token}"
    return (remote or "") in {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}


async def run_healthcheck() -> None:
    """Healthcheck + метрики Prometheus на одном порту."""
    from aiohttp import web
    import metrics as m

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "bot": bot_username or "starting"})

    async def metrics_endpoint(request: web.Request) -> web.Response:
        if not _metrics_allowed(
            request.remote,
            request.headers.get("Authorization"),
            config.METRICS_TOKEN,
        ):
            return web.Response(status=403, text="forbidden\n")
        return web.Response(text=m.render(), content_type="text/plain; version=0.0.4")

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/", health)
    if config.METRICS_ENABLED:
        app.router.add_get("/metrics", metrics_endpoint)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.HEALTH_PORT)
    await site.start()
    log.info("Healthcheck/метрики слушают на http://0.0.0.0:%s/ (metrics: %s)",
             config.HEALTH_PORT, "on" if config.METRICS_ENABLED else "off")


async def backup_worker() -> None:
    """Периодически создаёт бэкап SQLite БД (VACUUM INTO)."""
    import os
    backup_dir = os.path.join(os.path.dirname(config.DB_PATH) or ".", "backups")
    os.makedirs(backup_dir, exist_ok=True)
    interval = 6 * 3600  # каждые 6 часов
    keep = 7  # хранить 7 бэкапов
    while True:
        try:
            await asyncio.sleep(interval)
            from db import conn
            ts = time.strftime("%Y%m%d_%H%M%S")
            dest = os.path.join(backup_dir, f"bot_{ts}.db")
            async with conn() as db:
                await db.execute("VACUUM INTO ?", (dest,))
            # удаляем старые бэкапы
            files = sorted(
                f for f in os.listdir(backup_dir) if f.startswith("bot_") and f.endswith(".db")
            )
            for old in files[:-keep]:
                try:
                    os.remove(os.path.join(backup_dir, old))
                except OSError:
                    pass
            log.info("Бэкап БД создан: %s", dest)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("backup_worker")


async def _probe_recurring_worker() -> None:
    """При старте проверяет, поддерживает ли магазин ЮKassa рекуррентные платежи.

    Пока не поддерживает — автопродление невозможно (карта не сохраняется),
    и интерфейс честно предупреждает об этом.
    """
    try:
        from payments import probe_recurring_support
        supported = await probe_recurring_support()
        if supported is False:
            log.warning(
                "ЮKassa: магазин не поддерживает рекуррентные платежи — "
                "автопродление сейчас недоступно, автопродление включится "
                "после подключения рекуррентов в личном кабинете ЮKassa"
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("probe_recurring")


async def janitor_worker() -> None:
    """Периодически чистит временные записи: запросы на вход, старые сессии сайта."""
    interval = 600  # каждые 10 минут
    while True:
        try:
            await asyncio.sleep(interval)
            from db import cleanup_login_requests
            removed = await cleanup_login_requests(max_age=3600)
            if removed:
                log.info("janitor: удалено старых запросов на вход: %s", removed)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("janitor_worker")


# ---------- мониторинг панели ----------

_panel_fail_counts: dict[int, int] = defaultdict(int)   # server_id -> подряд неудач
_panel_alerted: dict[int, bool] = defaultdict(bool)     # server_id -> алерт уже отправлен


async def panel_health_worker() -> None:
    """Периодически пингует панели. При N подряд неудач — алерт админам, при восстановлении — отбивка."""
    import metrics as m
    while True:
        try:
            await asyncio.sleep(config.PANEL_CHECK_INTERVAL)
            servers = await list_servers()
            for server in servers:
                sid = server["id"]
                try:
                    client = await get_client(sid)
                    t0 = time.monotonic()
                    await client.get_users()
                    latency = time.monotonic() - t0
                    m.set_gauge(m.PANEL_UP, 1)
                    m.observe(m.PANEL_LATENCY_SECONDS, latency)
                    if _panel_fail_counts[sid] > 0 or _panel_alerted[sid]:
                        log.info("Панель сервера %s восстановилась (latency %.2fs)", sid, latency)
                        await _notify_admins(
                            f"✅ Панель сервера <b>{html.escape(str(server.get('name') or sid))}</b> "
                            f"снова доступна (latency {latency:.2f}s)."
                        )
                    _panel_fail_counts[sid] = 0
                    _panel_alerted[sid] = False
                except Exception as e:
                    _panel_fail_counts[sid] += 1
                    m.set_gauge(m.PANEL_UP, 0)
                    log.warning("Панель сервера %s недоступна (%d подряд): %s",
                                sid, _panel_fail_counts[sid], e)
                    if _panel_fail_counts[sid] >= config.PANEL_FAIL_ALERT_THRESHOLD and not _panel_alerted[sid]:
                        _panel_alerted[sid] = True
                        await _notify_admins(
                            f"🚨 Панель сервера <b>{html.escape(str(server.get('name') or sid))}</b> "
                            f"недоступна уже {_panel_fail_counts[sid]} проверок подряд!\n"
                            f"<code>{html.escape(str(server.get('base_url') or ''))}</code>\n"
                            f"Ошибка: {html.escape(str(e)[:300])}"
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("panel_health_worker")


async def _notify_admins(text: str) -> None:
    """Отправить сообщение всем админам из ADMIN_IDS."""
    if not bot:
        return
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass


async def refresh_user_metrics() -> None:
    """Обновляет gauge метрики пользователей (вызывается из poller)."""
    import metrics as m
    try:
        active = await get_active_customers()
        all_c = await get_all_customers()
        m.set_gauge(m.USERS_ACTIVE, len(active))
        m.set_gauge(m.USERS_TOTAL, len(all_c))
    except Exception:
        pass


# ---------- запуск ----------

async def main() -> None:
    global bot, bot_username, redis_client
    errors = config.validate()
    if errors:
        raise SystemExit("Ошибка конфигурации:\n- " + "\n- ".join(errors))

    await init_db()
    await seed_default_server()
    await seed_default_plans()

    # Инициализация Redis: нужен для Subscription Proxy (SUB_DOMAIN) или RedisStorage (USE_REDIS_STORAGE)
    if config.SUB_DOMAIN or config.USE_REDIS_STORAGE:
        redis_client = redis.from_url(config.REDIS_URL, encoding="utf-8", decode_responses=True)
        try:
            await redis_client.ping()
            log.info("Redis подключен: %s", config.REDIS_URL)
        except Exception as e:
            log.warning("Redis недоступен (%s): Subscription Proxy и RedisStorage отключены: %s",
                        config.REDIS_URL, e)
            redis_client = None

    # Миграция: генерируем sub_id для существующих пользователей без него
    if config.SUB_DOMAIN and redis_client:
        await migrate_sub_ids()

    # Миграция: добиваем VK-хеши до 4 у существующих клиентов (фоново)
    if not config.WDTT_VK_HASH:
        async def _bg_backfill() -> None:
            try:
                await asyncio.sleep(20)  # даём боту подняться
                await backfill_vk_hashes()
            except Exception as e:
                log.warning("фоновый backfill VK-хешей: %s", e)
        asyncio.create_task(_bg_backfill())

    bot = Bot(token=config.TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    bot_username = me.username

    await bot.delete_my_commands()

    # FSM storage: Redis (multi-instance) или Memory (single-instance)
    if config.USE_REDIS_STORAGE and redis_client:
        from aiogram.fsm.storage.redis import RedisStorage
        storage = RedisStorage(redis=redis_client)
        log.info("FSM storage: Redis")
    else:
        storage = MemoryStorage()
        log.info("FSM storage: Memory")
    dp = Dispatcher(storage=storage)
    dp.include_router(router)

    # Middleware: счётчик входящих сообщений для метрик
    from aiogram import BaseMiddleware
    import metrics as m

    class MetricsMiddleware(BaseMiddleware):
        async def __call__(self, handler, event, data):
            m.inc_counter(m.MESSAGES_TOTAL)
            try:
                return await handler(event, data)
            except Exception:
                m.inc_counter(m.ERRORS_TOTAL)
                raise

    dp.message.middleware(MetricsMiddleware())

    # Собираем все фоновые задачи для корректного завершения
    bg_tasks = [
        asyncio.create_task(poller()),
        asyncio.create_task(expiry_worker()),
        asyncio.create_task(autorenew_worker()),
        asyncio.create_task(run_healthcheck()),
        asyncio.create_task(backup_worker()),
        asyncio.create_task(janitor_worker()),
        asyncio.create_task(panel_health_worker()),
        asyncio.create_task(_probe_recurring_worker()),
    ]
    if config.SUB_DOMAIN and redis_client:
        bg_tasks.append(asyncio.create_task(sync_panel_worker()))
    if config.WEBHOOK_ENABLED:
        bg_tasks.append(asyncio.create_task(run_webhook()))
    if config.WEBAPP_ENABLED:
        from webapp import run_webapp
        bg_tasks.append(asyncio.create_task(run_webapp()))
    if config.WEB_ENABLED:
        from web_app import run_site
        bg_tasks.append(asyncio.create_task(run_site()))

    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Бот @%s запущен", bot_username)
    try:
        await dp.start_polling(bot)
    finally:
        # Graceful shutdown: отменяем фоновые задачи
        log.info("Остановка фоновых задач...")
        for t in bg_tasks:
            t.cancel()
        await asyncio.gather(*bg_tasks, return_exceptions=True)
        # Закрываем HTTP-клиент ЮKassa
        try:
            from payments import close_http_client
            await close_http_client()
        except Exception:
            pass
        for c in clients.values():
            await c.close()
        if redis_client:
            await redis_client.close()
        await bot.session.close()
        log.info("Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
