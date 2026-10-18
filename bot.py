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
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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

INSTRUCTION_TEXT = (
    "📖 <b>Подключение к WDTT</b>\n\n"
    "1. Установите клиент для вашей платформы.\n"
    "2. Скопируйте вашу ссылку <code>wdtt://</code> (профиль → ссылка).\n"
    "3. Вставьте её в клиент и подключитесь — устройства привяжутся "
    "автоматически (до лимита вашего тарифа).\n\n"
    "Клиенты:\n"
    "• Android — <b>WDTT</b> / qWDTT\n"
    "• iOS — <b>VK Turn Proxy</b>\n"
    "• Windows / Linux — <b>WDTT</b> или <b>PWDTT</b> "
    "(<code>pwdtt-client-*</code> из релизов WDTT)"
)

HELP_TEXT = (
    "ℹ️ <b>Как пользоваться ботом</b>\n\n"
    "1. Нажмите «Купить доступ» и выберите тариф.\n"
    "2. Оплатите через ЮKassa по ссылке.\n"
    "3. После оплаты бот пришлёт ссылку <code>wdtt://</code> для подключения.\n\n"
    "🎁 В меню доступен бесплатный пробный период (один раз).\n"
    f"👥 Пригласите друга (/ref) — получите +{config.REFERRAL_REWARD_DAYS} дня после его первой оплаты.\n\n"
    "Команды: /start, /buy, /status, /link, /ref, /help"
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
        raise WdttError("Не настроен ни один сервер WDTT (/add_server)")
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
            await client.update_user(old_password=pwd, expires_at=expires,
                                     total_gb=traffic, max_devices=devices)
            await client.reset_traffic(pwd)
        except WdttError as e:
            log.warning("Продление tg %s не удалось (%s), создаём новый ключ", telegram_id, e)
            pwd = await client.add_user(comment=_comment_for(telegram_id, username),
                                        expires_at=expires, total_gb=traffic, max_devices=devices)
    else:
        server_id = await pick_least_loaded_server_id()
        client = await get_client(server_id)
        expires = now + days * 86400
        pwd = await client.add_user(comment=_comment_for(telegram_id, username),
                                    expires_at=expires, total_gb=traffic, max_devices=devices)

    await upsert_customer(telegram_id, pwd, expires, plan["id"], server_id, username=username)
    return client, pwd, expires


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
                f"только на активную подписку — продлите свою (/buy), чтобы не терять их.",
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


# ---------- меню ----------

def nav_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛒 Купить"), KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="👥 Рефералы"), KeyboardButton(text="📖 Инструкция")],
            [KeyboardButton(text="❓ Помощь"), KeyboardButton(text="🏠 Главное меню")],
        ],
        resize_keyboard=True,
    )


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🎁 Пробный период ({TRIAL['days']} дн.)", callback_data="trial")],
        [InlineKeyboardButton(text="🛒 Купить доступ", callback_data="buy")],
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="profile")],
        [InlineKeyboardButton(
            text=f"👥 Пригласить друга (+{config.REFERRAL_REWARD_DAYS} дн.)", callback_data="ref")],
        [InlineKeyboardButton(text="📖 Инструкция", callback_data="instr")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="help")],
    ])


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="menu")],
    ])


def instruction_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🪟 Windows", callback_data="instr_win"),
         InlineKeyboardButton(text="🤖 Android", callback_data="instr_and")],
        [InlineKeyboardButton(text="🍎 iOS", callback_data="instr_ios"),
         InlineKeyboardButton(text="🐧 Linux", callback_data="instr_linux")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="menu")],
    ])


async def plans_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"{p['name']} — {fmt_price(p['price'])} ₽", callback_data=f"plan:{p['id']}",
        )]
        for p in await list_plans()
    ]
    rows.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- профиль и статус ----------

async def profile_text(telegram_id: int) -> str:
    customer = await get_customer(telegram_id)
    now = int(time.time())
    if not customer or not customer["wdtt_password"]:
        return "У вас нет активной подписки. Выберите тариф: /buy или подарочный 🎁 пробный период в меню."
    expires_at = int(customer["expires_at"] or 0)
    if expires_at <= now:
        return "Ваша подписка истекла. Продлить: /buy"

    plan = await get_plan(customer["plan_id"]) or ALL_PLANS_BY_ID.get(customer["plan_id"])
    plan_name = plan["name"] if plan else customer["plan_id"] or "—"
    diff = expires_at - now
    days, hours = diff // 86400, (diff % 86400) // 3600

    link = None
    try:
        client = await get_client(customer["server_id"])
        link = await client.find_link(customer["wdtt_password"])
    except Exception:
        log.warning("не удалось получить ссылку для tg %s", telegram_id)

    lines = [
        f"📊 <b>Ваша подписка:</b> {html.escape(str(plan_name))}",
        f"📅 Действует до: {fmt_date(expires_at)}",
        f"⏳ Осталось: {days} дн. {hours} ч",
    ]
    if plan:
        lines.append(f"📶 Трафик: {fmt_traffic(plan.get('traffic_gb'))} · Устройств: {plan.get('max_devices', 1)}")
    if link:
        lines.append(f"\n🔗 Ссылка подключения:\n<code>{html.escape(link)}</code>")
    else:
        lines.append("\n🔗 Ссылка временно недоступна — попробуйте позже (/link).")
    return "\n".join(lines)


# ---------- команды клиента ----------

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
        reply_markup=main_menu(),
    )



@router.message(Command("buy"))
async def cmd_buy(message: Message) -> None:
    await message.answer("Выберите тариф:", reply_markup=await plans_keyboard())


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    await message.answer(await profile_text(message.from_user.id))


@router.message(Command("link"))
async def cmd_link(message: Message) -> None:
    customer = await get_customer(message.from_user.id)
    now = int(time.time())
    if not customer or not customer["wdtt_password"] or int(customer["expires_at"] or 0) <= now:
        await message.answer("У вас нет активной подписки. Оформить: /buy")
        return
    try:
        client = await get_client(customer["server_id"])
        link = await client.find_link(customer["wdtt_password"])
    except Exception:
        log.exception("ошибка получения ссылки для tg %s", message.from_user.id)
        link = None
    if not link:
        await message.answer("⚠️ Не удалось получить ссылку. Попробуйте позже или напишите админу.")
        return
    await message.answer(
        f"🔗 Ваша ссылка подключения:\n<code>{html.escape(link)}</code>",
        reply_markup=back_menu(),
    )


@router.message(Command("ref"))
async def cmd_ref(message: Message) -> None:
    await message.answer(await ref_text(message.from_user.id))


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


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=back_menu())


# ---------- кнопки навигационного меню ----------

@router.message(F.text == "🛒 Купить")
async def nav_buy(message: Message) -> None:
    await message.answer("Выберите тариф:", reply_markup=await plans_keyboard())


@router.message(F.text == "👤 Профиль")
async def nav_profile(message: Message) -> None:
    await message.answer(await profile_text(message.from_user.id))


@router.message(F.text == "👥 Рефералы")
async def nav_ref(message: Message) -> None:
    await message.answer(await ref_text(message.from_user.id))


@router.message(F.text == "📖 Инструкция")
async def nav_instruction(message: Message) -> None:
    await message.answer(INSTRUCTION_TEXT, reply_markup=instruction_menu())


@router.message(F.text == "❓ Помощь")
async def nav_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=back_menu())


@router.message(F.text == "🏠 Главное меню")
async def nav_main(message: Message) -> None:
    await message.answer("Главное меню:", reply_markup=main_menu())


# ---------- callback-кнопки ----------

@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery) -> None:
    await cb.message.edit_text("Главное меню:", reply_markup=main_menu())
    await cb.answer()


@router.callback_query(F.data == "buy")
async def cb_buy(cb: CallbackQuery) -> None:
    await cb.message.edit_text("Выберите тариф:", reply_markup=await plans_keyboard())
    await cb.answer()


@router.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery) -> None:
    await cb.message.edit_text(await profile_text(cb.from_user.id), reply_markup=back_menu())
    await cb.answer()


@router.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery) -> None:
    await cb.message.edit_text(HELP_TEXT, reply_markup=back_menu())
    await cb.answer()


@router.callback_query(F.data == "ref")
async def cb_ref(cb: CallbackQuery) -> None:
    await cb.message.edit_text(await ref_text(cb.from_user.id), reply_markup=back_menu())
    await cb.answer()


@router.callback_query(F.data == "instr")
async def cb_instr(cb: CallbackQuery) -> None:
    await cb.message.edit_text(INSTRUCTION_TEXT, reply_markup=instruction_menu())
    await cb.answer()


@router.callback_query(F.data.startswith("instr_"))
async def cb_instruction_device(cb: CallbackQuery) -> None:
    device = cb.data[len("instr_"):]
    instructions = {
        "win": (
            "🪟 <b>Windows</b>\n\n"
            "1. Установите клиент <b>WDTT для Windows</b> или <b>PWDTT</b> "
            "(<code>pwdtt-client-*</code> из <a href='https://github.com/ildarmaga/wdtt/releases/latest'>релизов WDTT</a>).\n"
            "2. Скопируйте ссылку из профиля (👤).\n"
            "3. Вставьте её в клиент и нажмите «Подключиться»."
        ),
        "and": (
            "🤖 <b>Android</b>\n\n"
            "1. Установите приложение <b>WDTT</b> (или qWDTT).\n"
            "2. Скопируйте ссылку из профиля (👤).\n"
            "3. Добавьте конфигурацию по ссылке и подключитесь."
        ),
        "ios": (
            "🍎 <b>iOS</b>\n\n"
            "1. Установите <b>VK Turn Proxy</b> из App Store.\n"
            "2. Скопируйте ссылку из профиля (👤).\n"
            "3. Импортируйте ссылку и включите VPN."
        ),
        "linux": (
            "🐧 <b>Linux</b>\n\n"
            "1. Скачайте <b>PWDTT</b> (<code>pwdtt-client-*</code> из релизов WDTT).\n"
            "2. Скопируйте ссылку из профиля (👤).\n"
            "3. Вставьте ссылку в клиент и подключитесь."
        ),
    }
    text = instructions.get(device, "⚠️ Инструкция не найдена.")
    await cb.message.edit_text(text, reply_markup=instruction_menu())
    await cb.answer()


@router.callback_query(F.data == "trial")
async def cb_trial(cb: CallbackQuery) -> None:
    if await has_used_trial(cb.from_user.id):
        await cb.answer("Вы уже использовали пробный период!", show_alert=True)
        return
    customer = await get_customer(cb.from_user.id)
    now = int(time.time())
    if customer and customer["wdtt_password"] and int(customer["expires_at"] or 0) > now:
        await cb.answer("У вас уже есть активная подписка!", show_alert=True)
        return

    try:
        client, pwd, _ = await grant_access(cb.from_user.id, cb.from_user.username, TRIAL)
    except Exception as e:
        log.exception("ошибка выдачи пробного периода tg %s", cb.from_user.id)
        await cb.answer(f"Ошибка создания пробного периода: {e}", show_alert=True)
        return

    await mark_trial_used(cb.from_user.id)
    link = await client.find_link(pwd)
    await cb.message.edit_text(
        f"🎁 <b>Пробный период активирован!</b>\n\n"
        f"Срок: {TRIAL['days']} дн. · Трафик: {fmt_traffic(TRIAL['traffic_gb'])} · "
        f"Устройств: {TRIAL['max_devices']}\n\n"
        + (f"Ссылка подключения:\n<code>{html.escape(link)}</code>\n\n" if link else "")
        + "Инструкция: /help",
        reply_markup=back_menu(),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("plan:"))
async def cb_plan(cb: CallbackQuery) -> None:
    plan_id = cb.data.split(":", 1)[1]
    plan = await get_plan(plan_id) or ALL_PLANS_BY_ID.get(plan_id)
    if not plan:
        await cb.answer("Тариф не найден.", show_alert=True)
        return
    try:
        payment = await create_payment(plan, cb.from_user.id)
    except Exception:
        log.exception("не удалось создать платёж (тариф %s)", plan_id)
        await cb.answer("Не удалось создать платёж. Попробуйте позже.", show_alert=True)
        return
    if not payment.get("url"):
        await cb.answer("ЮKassa не вернула ссылку на оплату. Попробуйте позже.", show_alert=True)
        return

    await add_payment(
        telegram_id=cb.from_user.id,
        username=cb.from_user.username,
        payment_id=payment["id"],
        plan_id=str(plan["id"]),
        amount=f"{float(plan['price']):.2f}",
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"💳 Оплатить {fmt_price(plan['price'])} ₽", url=payment["url"],
        )],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="menu")],
    ])
    await cb.message.edit_text(
        f"💳 <b>Оплата тарифа «{html.escape(plan['name'])}»</b>\n\n"
        f"Сумма: {fmt_price(plan['price'])} ₽\n"
        f"Срок: {plan['days']} дн. · Трафик: {fmt_traffic(plan.get('traffic_gb'))}\n\n"
        "После оплаты доступ придёт автоматически (обычно в течение минуты).\n"
        f"Если вы уже платили — подписка продлится с текущей даты окончания.",
        reply_markup=keyboard,
    )
    await cb.answer()


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

        link = None
        try:
            link = await client.find_link(pwd)
        except Exception:
            log.warning("не удалось получить ссылку после оплаты %s", payment_id)
        message_text = (
            "✅ <b>Оплата прошла успешно!</b>\n\n"
            f"Подписка «{html.escape(plan['name'])}» активирована до {fmt_date(expires)}.\n"
            + (f"\n🔗 Ссылка подключения:\n<code>{html.escape(link)}</code>\n" if link else "")
            + "\nИнструкция по подключению — /help"
        )
        try:
            await bot.send_message(telegram_id, message_text, reply_markup=back_menu())
        except Exception:
            log.warning("не удалось отправить доступ клиенту %s", telegram_id)

        await notify_admin(
            f"💰 Оплата: {fmt_price(row['amount'])} ₽ — {html.escape(plan['name'])}\n"
            f"Клиент: {telegram_id}" + (f" (@{html.escape(row['username'])})" if row.get("username") else "")
        )
        await grant_referral_reward(telegram_id)


# ---------- админ-команды ----------

@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
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
        f"Выручка: {fmt_price(s['revenue'])} ₽\n\n"
        "Команды: /users, /servers, /add_server, /del_server, /revoke, /extend",
    )


@router.message(Command("users"))
async def cmd_users(message: Message) -> None:
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


@router.message(Command("revoke"))
async def cmd_revoke(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id):
        return
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Использование: /revoke <telegram_id>")
        return
    telegram_id = int(command.args.strip())
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


@router.message(Command("extend"))
async def cmd_extend(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        await message.answer("Использование: /extend <telegram_id> <дней>")
        return
    telegram_id, days = int(parts[0]), int(parts[1])
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


@router.message(Command("servers"))
async def cmd_servers(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    servers = await list_servers()
    if not servers:
        await message.answer("Серверы не настроены. Добавьте: /add_server")
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


@router.message(Command("add_server"))
async def cmd_add_server(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = (command.args or "").split()
    if len(parts) != 4:
        await message.answer(
            "Использование: /add_server <имя> <base_url> <логин> <пароль>\n"
            "Пример: /add_server de2 https://5.6.7.8:2860/wdtt admin secret"
        )
        return
    name, base_url, username, password = parts
    if not base_url.startswith(("http://", "https://")):
        await message.answer("base_url должен начинаться с https:// (или http:// для теста).")
        return
    verify_ssl = base_url.startswith("https://")
    probe = WdttClient(base_url, username, password, verify_ssl=verify_ssl)
    try:
        await probe.login()
    except Exception as e:
        await message.answer(f"⚠️ Не удалось войти в панель: {e}")
        return
    finally:
        await probe.close()
    server_id = await add_server(name, base_url, username, password, verify_ssl)
    await message.answer(
        f"✅ Сервер «{html.escape(name)}» добавлен (#{server_id}). "
        "Новые клиенты будут распределяться на наименее загруженный сервер."
    )


@router.message(Command("del_server"))
async def cmd_del_server(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id):
        return
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await message.answer("Использование: /del_server <id>")
        return
    server_id = int(raw)
    counts = await server_active_counts()
    if counts.get(server_id, 0) > 0:
        await message.answer(
            f"⚠️ На сервере #{server_id} ещё {counts[server_id]} активных клиентов — сначала отзовите их (/revoke)."
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
                        "⏰ Ваша подписка на WDTT истекла. Продлить: /buy",
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

    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="buy", description="Купить доступ"),
        BotCommand(command="status", description="Моя подписка"),
        BotCommand(command="link", description="Моя ссылка"),
        BotCommand(command="ref", description="Пригласить друга"),
        BotCommand(command="help", description="Помощь"),
        BotCommand(command="admin", description="Админ: статистика"),
        BotCommand(command="users", description="Админ: клиенты"),
        BotCommand(command="servers", description="Админ: серверы"),
        BotCommand(command="add_server", description="Админ: добавить сервер"),
        BotCommand(command="del_server", description="Админ: удалить сервер"),
        BotCommand(command="revoke", description="Админ: отозвать доступ"),
        BotCommand(command="extend", description="Админ: продлить доступ"),
    ])

    dp = Dispatcher()
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
