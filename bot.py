"""Telegram-бот продажи подписок на WDTT с оплатой через ЮKassa.

Клиент платит -> бот создаёт/продлевает пользователя в панели WDTT
(https://github.com/ildarmaga/wdtt) и выдаёт ссылку wdtt://.
"""

import asyncio
import html
import logging
import time
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

import config
from db import (
    add_payment,
    add_referral,
    add_server,
    backfill_customer_servers,
    claim_referral_reward,
    clear_customer,
    delete_plan,
    delete_server,
    get_active_customers,
    get_customer,
    get_expired_customers,
    get_payment,
    get_pending_payments,
    get_plan,
    get_referrals_count,
    get_server,
    has_used_trial,
    init_db,
    list_plans,
    list_servers,
    mark_trial_used,
    server_active_counts,
    set_payment_status,
    stats,
    upsert_customer,
    upsert_plan,
)
from payments import create_payment, fetch_payment
from plans import ALL_PLANS_BY_ID, PLANS, TRIAL
from wdtt import WdttClient, WdttError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

bot: Bot | None = None
bot_username: str | None = None
fulfill_lock = asyncio.Lock()
clients: dict[int, WdttClient] = {}

router = Router()


class AdminStates(StatesGroup):
    revoke_id = State()
    extend_data = State()
    add_server_name = State()
    add_server_url = State()
    add_server_login = State()
    add_server_password = State()
    del_server_id = State()
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
    "📖 Инструкция", "❓ Помощь", "🏠 Главное меню", "📊 Статистика",
    "👥 Клиенты", "⛔ Отозвать", "➕ Продлить", "🖥 Серверы",
    "➕ Добавить сервер", "🗑 Удалить сервер",
    "🗂 Тарифы", "➕ Добавить тариф", "✏️ Изменить тариф", "🗑 Удалить тариф",
}

INSTRUCTION_TEXT = (
    "📖 <b>Подключение к WDTT</b>\n\n"
    "1. Установите клиент для вашей платформы.\n"
    "2. Скопируйте вашу ссылку <code>wdtt://</code> (кнопка 👤 Моя подписка).\n"
    "3. Вставьте её в клиент и подключитесь — устройства привяжутся "
    "автоматически (до лимита вашего тарифа).\n\n"
    "🪟 <b>Windows</b> — установите <b>WDTT для Windows</b> или <b>PWDTT</b> "
    "(<code>pwdtt-client-*</code> из "
    "<a href='https://github.com/ildarmaga/wdtt/releases/latest'>релизов WDTT</a>), "
    "вставьте ссылку и нажмите «Подключиться».\n\n"
    "🤖 <b>Android</b> — установите приложение "
    "<a href='https://github.com/amurcanov/csqtt/releases/download/v2.1.9/CSQTT-universal.apk'>CSQTT</a> "
    "или <a href='https://github.com/amurcanov/proxy-turn-vk-android/releases/download/v1.2.4/WDTT-universal.apk'>WDTT</a>, "
    "добавьте конфигурацию по ссылке и подключитесь.\n\n"
    "🍎 <b>iOS</b> — загрузите приложение TestFlight из App Store на устройство, "
    "затем откройте <a href='https://testflight.apple.com/join/ANm6cmDv'>эту ссылку</a>, "
    "установите клиент, импортируйте ссылку и включите VPN.\n\n"
    "🐧 <b>Linux</b> — скачайте <b>PWDTT</b> (<code>pwdtt-client-*</code>), "
    "вставьте ссылку в клиент и подключитесь."
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
    return "безлимит" if not gb else f"{int(gb)} ГБ"


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


async def pick_least_loaded_server_id() -> int | None:
    """Сервер с наименьшим числом активных клиентов (новые покупатели)."""
    servers = await list_servers()
    if not servers:
        return None
    counts = await server_active_counts()
    return min(servers, key=lambda s: (counts.get(s["id"], 0), s["id"]))["id"]


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
    if await list_plans():
        return
    for p in PLANS:
        await upsert_plan(p["id"], p["name"], int(p["days"]), float(p["price"]),
                          int(p["traffic_gb"]), int(p["max_devices"]))
    log.info("Заполнены тарифы по умолчанию: %s", ", ".join(p["id"] for p in PLANS))


# ---------- выдача доступа ----------

def _comment_for(telegram_id: int, username: str | None) -> str:
    return f"tg:{telegram_id}" + (f" @{username}" if username else "")


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
        except WdttError as e:
            log.warning("Продление tg %s не удалось (%s), создаём новый ключ", telegram_id, e)
            pwd = await client.add_user(comment=_comment_for(telegram_id, username),
                                        expires_at=expires, total_gb=traffic, max_devices=devices,
                                        vk_hash=config.WDTT_VK_HASH)
    else:
        server_id = await pick_least_loaded_server_id()
        client = await get_client(server_id)
        expires = now + days * 86400
        pwd = await client.add_user(comment=_comment_for(telegram_id, username),
                                    expires_at=expires, total_gb=traffic, max_devices=devices,
                                    vk_hash=config.WDTT_VK_HASH)

    await _ensure_vk_hash(client, pwd)
    await upsert_customer(telegram_id, pwd, expires, plan["id"], server_id, username=username)
    return client, pwd, expires


async def _ensure_vk_hash(client: WdttClient, pwd: str) -> None:
    """Гарантирует vk_hash у пользователя: задать из WDTT_VK_HASH либо создать VK-вызов.

    Сбой не прерывает выдачу — в ссылках останется плейсхолдер VK_HASH (не фатально).
    """
    if config.WDTT_VK_HASH:
        return
    try:
        info = await client.get_user_info(pwd) or {}
        if WdttClient._clean_hash_pieces(str(info.get("vk_hash") or "")):
            return  # настоящий хэш уже есть
        await client.create_vk_call(pwd)
    except Exception as e:
        log.warning("VK-вызов для %s не создан (%s) — hash останется плейсхолдером", pwd[:2], e)


async def grant_referral_reward(referred_id: int) -> None:
    """+N дней пригласившему после первой оплаты приглашённого."""
    days = config.REFERRAL_REWARD_DAYS
    if days <= 0:
        return
    referrer_id = await claim_referral_reward(referred_id)
    if not referrer_id:
        return
    referrer = await get_customer(referrer_id)
    now = int(time.time())
    if not referrer or not referrer["wdtt_password"] or int(referrer["expires_at"] or 0) <= now:
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
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def admin_menu() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="👥 Клиенты")],
        [KeyboardButton(text="⛔ Отозвать"), KeyboardButton(text="➕ Продлить")],
        [KeyboardButton(text="🖥 Серверы"), KeyboardButton(text="➕ Добавить сервер")],
        [KeyboardButton(text="🗑 Удалить сервер")],
        [KeyboardButton(text="🗂 Тарифы"), KeyboardButton(text="➕ Добавить тариф")],
        [KeyboardButton(text="✏️ Изменить тариф"), KeyboardButton(text="🗑 Удалить тариф")],
        [KeyboardButton(text="🏠 Главное меню")],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def plans_reply_keyboard(plans) -> ReplyKeyboardMarkup:
    PLAN_BUTTONS.clear()
    rows = []
    for p in plans:
        text = f"💳 {p['name']} — {fmt_price(p['price'])} ₽"
        PLAN_BUTTONS[text] = p["id"]
        rows.append([KeyboardButton(text=text)])
    rows.append([KeyboardButton(text="🏠 Главное меню")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


# ---------- профиль и статус ----------

async def _links_section(client, pwd: str) -> str | None:
    """Блок со ссылками подключения для iOS и Android.

    Платформенные ссылки берём со страницы подписки панели; если их нет —
    откатываемся на универсальную wdtt:// ссылку.
    """
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

    section = None
    try:
        client = await get_client(customer["server_id"])
        section = await _links_section(client, customer["wdtt_password"])
    except Exception:
        log.warning("не удалось получить ссылку для tg %s", telegram_id)

    lines = [
        f"📊 <b>Ваша подписка:</b> {html.escape(str(plan_name))}",
        f"📅 Действует до: {fmt_date(expires_at)}",
        f"⏳ Осталось: {days} дн. {hours} ч",
    ]
    if plan:
        lines.append(f"📶 Трафик: {fmt_traffic(plan.get('traffic_gb'))} · Устройств: {plan.get('max_devices', 1)}")
    if section:
        lines.append(f"\n🔗 Ссылки для подключения:\n{section}")
    else:
        lines.append("\n🔗 Ссылка временно недоступна — попробуйте позже (кнопка 👤 Моя подписка).")
    return "\n".join(lines)


# ---------- старт (единственный технический обработчик команды) ----------

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    # реферальная ссылка имеет вид https://t.me/bot?start=ref_<tg_id>
    args = (message.text or "").split()
    if len(args) > 1 and args[1].startswith("ref_"):
        raw = args[1][4:]
        if raw.isdigit() and int(raw) != message.from_user.id:
            await add_referral(message.from_user.id, int(raw))

    await message.answer(
        "👋 Привет! Это бот продажи доступа к <b>WDTT</b>.\n\n"
        "Здесь можно купить подписку (оплата через ЮKassa), активировать "
        "бесплатный пробный период и получить ссылку для подключения.\n\n"
        "Выберите действие:",
        reply_markup=await nav_menu(message.from_user.id),
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
    await message.answer(INSTRUCTION_TEXT)


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
    if await has_used_trial(user_id):
        await message.answer("Вы уже использовали пробный период!")
        return
    customer = await get_customer(user_id)
    now = int(time.time())
    if customer and customer["wdtt_password"] and int(customer["expires_at"] or 0) > now:
        await message.answer("У вас уже есть активная подписка!")
        return

    try:
        client, pwd, _ = await grant_access(user_id, message.from_user.username, TRIAL)
    except Exception as e:
        log.exception("ошибка выдачи пробного периода tg %s", user_id)
        await message.answer(f"Ошибка создания пробного периода: {e}")
        return

    await mark_trial_used(user_id)
    section = await _links_section(client, pwd)
    await message.answer(
        "🎁 <b>Пробный период активирован!</b>\n\n"
        f"Срок: {TRIAL['days']} дн. · Трафик: {fmt_traffic(TRIAL['traffic_gb'])} · "
        f"Устройств: {TRIAL['max_devices']}\n\n"
        + (f"Ссылки для подключения:\n{section}\n\n" if section else "")
        + "Инструкция — кнопка 📖 Инструкция."
    )


@router.message(F.text.startswith("💳 "), StateFilter(None))
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
    await message.answer(
        f"💳 <b>Оплата тарифа «{html.escape(plan['name'])}»</b>\n\n"
        f"Сумма: {fmt_price(plan['price'])} ₽\n"
        f"Срок: {plan['days']} дн. · Трафик: {fmt_traffic(plan.get('traffic_gb'))}\n\n"
        f"🔗 Ссылка для оплаты:\n{html.escape(payment['url'])}\n\n"
        "Оплатите по ссылке — после подтверждения доступ придёт автоматически "
        "(обычно в течение минуты).\n"
        "Если вы уже платили — подписка продлится с текущей даты окончания."
    )


@router.message(~StateFilter(None), F.text.in_(NAV_BUTTONS))
async def nav_button_during_state(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.text == "🏠 Главное меню":
        await message.answer("Главное меню:", reply_markup=await nav_menu(message.from_user.id))
    else:
        await message.answer("Действие отменено. Нажмите кнопку ещё раз.")


# ---------- исполнение платежей ----------

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

        if row["status"] == "pending":
            await set_payment_status(payment_id, "paid", paid_at=yk.get("paid_at"))

        telegram_id = int(row["telegram_id"])
        plan = await get_plan(row["plan_id"]) or ALL_PLANS_BY_ID.get(row["plan_id"])
        if not plan:
            await notify_admin(
                f"⚠️ Платёж {payment_id} оплачен, но тариф «{row['plan_id']}» не найден — "
                f"доступ клиенту {telegram_id} не выдан."
            )
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

        section = None
        try:
            section = await _links_section(client, pwd)
        except Exception:
            log.warning("не удалось получить ссылку после оплаты %s", payment_id)
        message_text = (
            "✅ <b>Оплата прошла успешно!</b>\n\n"
            f"Подписка «{html.escape(plan['name'])}» активирована до {fmt_date(expires)}.\n"
            + (f"\n🔗 Ссылки для подключения:\n{section}\n" if section else "")
            + "\nИнструкция по подключению — кнопка 📖 Инструкция"
        )
        try:
            await bot.send_message(telegram_id, message_text)
        except Exception:
            log.warning("не удалось отправить доступ клиенту %s", telegram_id)

        await notify_admin(
            f"💰 Оплата: {fmt_price(row['amount'])} ₽ — {html.escape(plan['name'])}\n"
            f"Клиент: {telegram_id}" + (f" (@{html.escape(row['username'])})" if row.get("username") else "")
        )
        await grant_referral_reward(telegram_id)


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
        await client.update_user(
            old_password=customer["wdtt_password"], expires_at=expires,
            total_gb=int(info.get("total_gb") or 0),
            max_devices=int(info.get("max_devices") or 1),
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
        except Exception:
            log.exception("poller loop")
        await asyncio.sleep(config.POLL_INTERVAL)


async def expiry_worker() -> None:
    """Раз в час удаляет истёкшие ключи из панели и уведомляет клиентов."""
    while True:
        try:
            for c in await get_expired_customers():
                try:
                    client = await get_client(c["server_id"])
                    await client.delete_user(c["wdtt_password"])
                except Exception:
                    log.exception("не удалось удалить истёкший ключ tg %s", c["telegram_id"])
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


async def on_webhook_payment(payment_id: str) -> None:
    try:
        await fulfill_payment(payment_id)
    except Exception:
        log.exception("webhook fulfill")


async def run_webhook() -> None:
    from aiohttp import web
    from webhook import make_webhook_app

    app = make_webhook_app(config.WEBHOOK_PATH, on_webhook_payment)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.WEBHOOK_HOST, config.WEBHOOK_PORT)
    await site.start()
    log.info("Webhook ЮKassa слушает на http://%s:%s%s",
             config.WEBHOOK_HOST, config.WEBHOOK_PORT, config.WEBHOOK_PATH)


# ---------- запуск ----------

async def main() -> None:
    global bot, bot_username
    errors = config.validate()
    if errors:
        raise SystemExit("Ошибка конфигурации:\n- " + "\n- ".join(errors))

    await init_db()
    await seed_default_server()
    await seed_default_plans()

    bot = Bot(token=config.TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    bot_username = me.username

    await bot.delete_my_commands()

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    asyncio.create_task(poller())
    asyncio.create_task(expiry_worker())
    if config.WEBHOOK_ENABLED:
        asyncio.create_task(run_webhook())

    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Бот @%s запущен", bot_username)
    try:
        await dp.start_polling(bot)
    finally:
        for c in clients.values():
            await c.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
