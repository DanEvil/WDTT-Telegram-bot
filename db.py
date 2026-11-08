"""Хранение данных: SQLite через aiosqlite."""
import secrets
import time
from contextlib import asynccontextmanager

import aiosqlite

from config import DB_PATH


@asynccontextmanager
async def conn():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        # Все запросы открывают своё соединение: без busy_timeout параллельная
        # запись падает с "database is locked", а NORMAL в WAL-режиме заметно
        # быстрее FULL и не приводит к порче БД.
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("PRAGMA synchronous=NORMAL")
        yield db
        await db.commit()
    finally:
        await db.close()


async def init_db() -> None:
    async with conn() as db:
        # WAL: чтение не блокирует запись (важно при фоновых воркерах).
        # Режим хранится в файле БД, поэтому достаточно выставить один раз.
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS customers (
                telegram_id  INTEGER PRIMARY KEY,
                username     TEXT,
                wdtt_password TEXT,
                expires_at   INTEGER NOT NULL DEFAULT 0,
                plan_id      TEXT,
                server_id    INTEGER,
                sub_id       TEXT,
                auto_renew   INTEGER NOT NULL DEFAULT 0,   -- 1 = автоматически продлевать на 1 месяц
                notify_hours INTEGER NOT NULL DEFAULT 0,   -- за сколько часов уведомлять о списании (0 = не уведомлять)
                payment_method_id TEXT,                     -- сохранённый способ оплаты ЮKassa (карта)
                renew_notified INTEGER NOT NULL DEFAULT 0,  -- уведомление о скором списании уже отправлено
                renew_charge_exp INTEGER NOT NULL DEFAULT 0, -- expires_at, за который уже пытались списать
                renew_charge_at INTEGER NOT NULL DEFAULT 0,  -- время последней попытки списания
                renew_charge_payment_id TEXT,               -- платёж автопродления (если списание создано)
                created_at   INTEGER NOT NULL
                        );

            CREATE TABLE IF NOT EXISTS payments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                username    TEXT,
                payment_id  TEXT NOT NULL UNIQUE,
                plan_id     TEXT NOT NULL,
                amount      TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',
                created_at  INTEGER NOT NULL,
                paid_at     INTEGER
            );

            CREATE TABLE IF NOT EXISTS trials (
                telegram_id INTEGER PRIMARY KEY,
                used_at     INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS referrals (
                referred_id  INTEGER PRIMARY KEY,
                referrer_id  INTEGER NOT NULL,
                created_at   INTEGER NOT NULL,
                rewarded     INTEGER NOT NULL DEFAULT 0,
                rewarded_at  INTEGER
            );

            CREATE TABLE IF NOT EXISTS servers (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                base_url    TEXT NOT NULL,
                username    TEXT NOT NULL,
                password    TEXT NOT NULL,
                verify_ssl  INTEGER NOT NULL DEFAULT 0,
                created_at  INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS plans (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                days        INTEGER NOT NULL,
                price       REAL NOT NULL,
                traffic_gb  INTEGER NOT NULL DEFAULT 0,
                max_devices INTEGER NOT NULL DEFAULT 1,
                sort_order  INTEGER NOT NULL DEFAULT 0,
                created_at  INTEGER NOT NULL
            );
            """
        )

        # Миграция: добавляем server_id в существующую таблицу customers
        cols = [r["name"] for r in (await (await db.execute("PRAGMA table_info(customers)")).fetchall())]
        if "server_id" not in cols:
            await db.execute("ALTER TABLE customers ADD COLUMN server_id INTEGER")
        # Миграция: добавляем sub_id в существующую таблицу customers
        if "sub_id" not in cols:
            await db.execute("ALTER TABLE customers ADD COLUMN sub_id TEXT")
        # Миграция: битовая маска отправленных напоминаний (за 3/2/1 день)
        if "reminder_sent" not in cols:
            await db.execute("ALTER TABLE customers ADD COLUMN reminder_sent INTEGER NOT NULL DEFAULT 0")
        # Миграция: было ли отправлено предупреждение о трафике (<150 МБ)
        if "traffic_notified" not in cols:
            await db.execute("ALTER TABLE customers ADD COLUMN traffic_notified INTEGER NOT NULL DEFAULT 0")
        # Миграция: реальный telegram_id для уведомлений (для сайт-аккаунтов, привязанных к TG)
        if "notify_tg_id" not in cols:
            await db.execute("ALTER TABLE customers ADD COLUMN notify_tg_id INTEGER")
        # Миграция: автопродление подписки (на 1 месяц) + уведомление о списании + сохранённый способ оплаты
        if "auto_renew" not in cols:
            await db.execute("ALTER TABLE customers ADD COLUMN auto_renew INTEGER NOT NULL DEFAULT 0")
        if "notify_hours" not in cols:
            await db.execute("ALTER TABLE customers ADD COLUMN notify_hours INTEGER NOT NULL DEFAULT 0")
        if "payment_method_id" not in cols:
            await db.execute("ALTER TABLE customers ADD COLUMN payment_method_id TEXT")
        if "renew_notified" not in cols:
            await db.execute("ALTER TABLE customers ADD COLUMN renew_notified INTEGER NOT NULL DEFAULT 0")
        # Миграция: защита от повторного списания при автопродлении
        if "renew_charge_exp" not in cols:
            await db.execute("ALTER TABLE customers ADD COLUMN renew_charge_exp INTEGER NOT NULL DEFAULT 0")
        if "renew_charge_at" not in cols:
            await db.execute("ALTER TABLE customers ADD COLUMN renew_charge_at INTEGER NOT NULL DEFAULT 0")
        if "renew_charge_payment_id" not in cols:
            await db.execute("ALTER TABLE customers ADD COLUMN renew_charge_payment_id TEXT")

        # Миграции таблицы servers: SSH-доступ для автоустановки панели
        scols = [r["name"] for r in (await (await db.execute("PRAGMA table_info(servers)")).fetchall())]
        if "ssh_host" not in scols:
            await db.execute("ALTER TABLE servers ADD COLUMN ssh_host TEXT")
        if "ssh_port" not in scols:
            await db.execute("ALTER TABLE servers ADD COLUMN ssh_port INTEGER")
        if "ssh_user" not in scols:
            await db.execute("ALTER TABLE servers ADD COLUMN ssh_user TEXT")
        if "ssh_password" not in scols:
            await db.execute("ALTER TABLE servers ADD COLUMN ssh_password TEXT")

        # VK-аккаунты (cookies профилей VK для создания хешей) и учёт выданных хешей
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS vk_accounts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                label       TEXT NOT NULL,
                cookies     TEXT NOT NULL,          -- JSON-массив cookies или строка "k=v; ..."
                active      INTEGER NOT NULL DEFAULT 1,
                created_at  INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS vk_issued (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id     INTEGER NOT NULL,
                vk_account_id INTEGER NOT NULL,
                user_password TEXT NOT NULL,
                vk_hash       TEXT NOT NULL,
                created_at    INTEGER NOT NULL
            );
            """
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_vk_issued_server ON vk_issued(server_id, vk_account_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_vk_issued_user ON vk_issued(user_password)")

        # Индексы для быстрого поиска (sync_panel_worker, sub_id lookups)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_customers_wdtt_password ON customers(wdtt_password)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_customers_sub_id ON customers(sub_id)")
        # Воркеры напоминаний/истечения и автопродления фильтруют по expires_at
        await db.execute("CREATE INDEX IF NOT EXISTS idx_customers_expires ON customers(expires_at)")
        # Личные кабинеты и webhook часто выбирают платежи по пользователю/статусу
        await db.execute("CREATE INDEX IF NOT EXISTS idx_payments_telegram ON payments(telegram_id, created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status, created_at)")

        # Таблицы сайта (личный кабинет): аккаунты и коды привязки Telegram
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS site_users (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                email          TEXT NOT NULL UNIQUE,
                username       TEXT,
                password_hash  TEXT NOT NULL,
                telegram_id    INTEGER UNIQUE,
                referral_code  TEXT NOT NULL UNIQUE,
                created_at     INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bind_codes (
                code         TEXT PRIMARY KEY,
                site_user_id INTEGER NOT NULL,
                created_at   INTEGER NOT NULL,
                used         INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        # Запросы на вход на сайт через Telegram (deep-link t.me/<bot>?start=web_login_<code>)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS login_requests (
                code       TEXT PRIMARY KEY,
                telegram_id INTEGER,
                username   TEXT,
                created_at INTEGER NOT NULL,
                used       INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_site_users_telegram ON site_users(telegram_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_bind_codes_site ON bind_codes(site_user_id)")

        # Таблица рефералов сайта (отдельная от тг-рефералов в таблице referrals)
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS site_referrals (
                referred_site_user_id  INTEGER PRIMARY KEY,
                referrer_site_user_id  INTEGER NOT NULL,
                created_at             INTEGER NOT NULL,
                rewarded               INTEGER NOT NULL DEFAULT 0,
                rewarded_at            INTEGER
            );
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_site_referrals_referrer ON site_referrals(referrer_site_user_id)"
        )
        
        
        # ---------- payments ----------
        
async def add_payment(telegram_id: int, username: str | None, payment_id: str,
                      plan_id: str, amount: str) -> None:
    async with conn() as db:
        await db.execute(
            "INSERT OR IGNORE INTO payments (telegram_id, username, payment_id, plan_id, amount, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (telegram_id, username, payment_id, plan_id, amount, int(time.time())),
        )


async def get_payment(payment_id: str) -> dict | None:
    async with conn() as db:
        cur = await db.execute("SELECT * FROM payments WHERE payment_id = ?", (payment_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def list_payments(telegram_id: int, limit: int = 20) -> list[dict]:
    """Последние платежи пользователя (новые сверху)."""
    async with conn() as db:
        cur = await db.execute(
            "SELECT * FROM payments WHERE telegram_id = ? ORDER BY created_at DESC LIMIT ?",
            (telegram_id, limit),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def set_payment_status(payment_id: str, status: str, paid_at: int | None = None) -> None:
    async with conn() as db:
        await db.execute(
            "UPDATE payments SET status = ?, paid_at = COALESCE(?, paid_at) WHERE payment_id = ?",
            (status, paid_at, payment_id),
        )


async def get_pending_payments(max_age_days: int = 7) -> list[dict]:
    """Неоплаченные ('pending') и оплаченные, но не выданные ('paid') платежи.

    Статусы: pending -> paid (деньги получены) -> fulfilled (доступ выдан);
    cancelled — платёж отменён клиентом/ЮKassa.

    Свежие ('paid') — всегда; старые 'pending' не старше max_age_days
    (заброшенные корзины не должны грузить API ЮKassa вечно).
    """
    cutoff = int(time.time()) - max_age_days * 86400
    async with conn() as db:
        cur = await db.execute(
            "SELECT * FROM payments WHERE (status = 'paid') OR "
            "(status = 'pending' AND created_at > ?)",
            (cutoff,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


# ---------- customers ----------

async def get_customer(telegram_id: int) -> dict | None:
    async with conn() as db:
        cur = await db.execute("SELECT * FROM customers WHERE telegram_id = ?", (telegram_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def upsert_customer(telegram_id: int, wdtt_password: str, expires_at: int,
                          plan_id: str | None, server_id: int | None = None,
                          username: str | None = None, sub_id: str | None = None) -> None:
    async with conn() as db:
        await db.execute(
            """
            INSERT INTO customers (telegram_id, username, wdtt_password, expires_at, plan_id, server_id, sub_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username      = COALESCE(excluded.username, customers.username),
                wdtt_password = excluded.wdtt_password,
                expires_at    = excluded.expires_at,
                plan_id       = excluded.plan_id,
                server_id     = excluded.server_id,
                sub_id        = COALESCE(excluded.sub_id, customers.sub_id),
                -- Сбрасываем метки напоминаний только при реальной смене срока
                -- или тарифа (продление/новый ключ). Иначе sync_panel_worker,
                -- вызываемый каждый час, стирал бы их и напоминания не уходили.
                reminder_sent = CASE
                    WHEN customers.expires_at = excluded.expires_at
                     AND COALESCE(customers.plan_id, '') = COALESCE(excluded.plan_id, '')
                    THEN customers.reminder_sent
                    ELSE 0 END,
                traffic_notified = CASE
                    WHEN customers.expires_at = excluded.expires_at
                     AND COALESCE(customers.plan_id, '') = COALESCE(excluded.plan_id, '')
                    THEN customers.traffic_notified
                    ELSE 0 END
            """,
            (telegram_id, username, wdtt_password, expires_at, plan_id, server_id, sub_id, int(time.time())),
        )


async def clear_customer(telegram_id: int) -> None:
    async with conn() as db:
        await db.execute(
            "UPDATE customers SET wdtt_password = NULL, expires_at = 0, plan_id = NULL, "
            "reminder_sent = 0, traffic_notified = 0 WHERE telegram_id = ?",
            (telegram_id,),
        )


# ---------- автопродление подписки ----------

async def set_auto_renew(telegram_id: int, auto_renew: bool, notify_hours: int = 0) -> None:
    """Включает/выключает автопродление и порог уведомления о списании (часы, 0 = не уведомлять)."""
    async with conn() as db:
        await db.execute(
            "UPDATE customers SET auto_renew = ?, notify_hours = ? WHERE telegram_id = ?",
            (1 if auto_renew else 0, max(0, int(notify_hours)), telegram_id),
        )


async def get_auto_renew_settings(telegram_id: int) -> dict:
    async with conn() as db:
        row = await (await db.execute(
            "SELECT auto_renew, notify_hours, payment_method_id FROM customers WHERE telegram_id = ?",
            (telegram_id,),
        )).fetchone()
    if not row:
        return {"auto_renew": False, "notify_hours": 0, "has_payment_method": False}
    return {
        "auto_renew": bool(row["auto_renew"]),
        "notify_hours": int(row["notify_hours"] or 0),
        "has_payment_method": bool(row["payment_method_id"]),
    }


async def set_payment_method(telegram_id: int, payment_method_id: str | None) -> None:
    async with conn() as db:
        await db.execute(
            "UPDATE customers SET payment_method_id = ? WHERE telegram_id = ?",
            (payment_method_id, telegram_id),
        )


async def list_autorenew_customers() -> list[dict]:
    """Активные клиенты с включённым автопродлением и сохранённым способом оплаты."""
    async with conn() as db:
        rows = await (await db.execute(
            "SELECT * FROM customers WHERE auto_renew = 1 AND payment_method_id IS NOT NULL "
            "AND wdtt_password IS NOT NULL AND expires_at > 0"
        )).fetchall()
    return [dict(r) for r in rows]


async def mark_renew_notified(telegram_id: int) -> None:
    async with conn() as db:
        await db.execute("UPDATE customers SET renew_notified = 1 WHERE telegram_id = ?", (telegram_id,))


async def clear_renew_notified(telegram_id: int) -> None:
    async with conn() as db:
        await db.execute("UPDATE customers SET renew_notified = 0 WHERE telegram_id = ?", (telegram_id,))


# ---------- защита от повторного списания (автопродление) ----------

async def get_renew_charge(telegram_id: int) -> dict | None:
    """Состояние последней попытки списания по автопродлению."""
    async with conn() as db:
        row = await (await db.execute(
            "SELECT renew_charge_exp, renew_charge_at, renew_charge_payment_id "
            "FROM customers WHERE telegram_id = ?", (telegram_id,)
        )).fetchone()
    return dict(row) if row else None


async def mark_renew_charge(telegram_id: int, expires_at: int, payment_id: str | None = None) -> None:
    """Фиксирует попытку списания за конкретный срок подписки.

    payment_id=None — попытка без созданного платежа (ошибка сети/карты).
    """
    async with conn() as db:
        await db.execute(
            "UPDATE customers SET renew_charge_exp = ?, renew_charge_at = ?, "
            "renew_charge_payment_id = COALESCE(?, renew_charge_payment_id) WHERE telegram_id = ?",
            (int(expires_at), int(time.time()), payment_id, telegram_id),
        )


async def clear_renew_charge(telegram_id: int) -> None:
    """Сбрасывает метку списания (например, после успешного продления)."""
    async with conn() as db:
        await db.execute(
            "UPDATE customers SET renew_charge_exp = 0, renew_charge_at = 0, "
            "renew_charge_payment_id = NULL WHERE telegram_id = ?", (telegram_id,)
        )


async def get_expired_customers() -> list[dict]:
    now = int(time.time())
    async with conn() as db:
        cur = await db.execute(
            "SELECT * FROM customers WHERE wdtt_password IS NOT NULL AND expires_at > 0 AND expires_at <= ?",
            (now,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_active_customers() -> list[dict]:
    now = int(time.time())
    async with conn() as db:
        cur = await db.execute(
            "SELECT * FROM customers WHERE wdtt_password IS NOT NULL AND expires_at > ? ORDER BY expires_at",
            (now,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def claim_reminder(telegram_id: int, days: int) -> bool:
    """Отметить напоминание «за N дней» отправленным.

    Битовая маска в customers.reminder_sent (бит 1 << N). True — отправлено впервые.
    """
    async with conn() as db:
        cur = await db.execute(
            "UPDATE customers SET reminder_sent = reminder_sent | (1 << ?) "
            "WHERE telegram_id = ? AND wdtt_password IS NOT NULL AND (reminder_sent & (1 << ?)) = 0",
            (days, telegram_id, days),
        )
        return cur.rowcount == 1


async def claim_traffic_notice(telegram_id: int) -> bool:
    """Пометить предупреждение «мало трафика» отправленным. True — отправлено впервые."""
    async with conn() as db:
        cur = await db.execute(
            "UPDATE customers SET traffic_notified = 1 "
            "WHERE telegram_id = ? AND wdtt_password IS NOT NULL AND traffic_notified = 0",
            (telegram_id,),
        )
        return cur.rowcount == 1


async def get_all_customers() -> list[dict]:
    async with conn() as db:
        cur = await db.execute("SELECT * FROM customers ORDER BY created_at DESC")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_customer_by_password(wdtt_password: str) -> dict | None:
    """Быстрый поиск клиента по паролю панели (использует индекс idx_customers_wdtt_password)."""
    async with conn() as db:
        cur = await db.execute("SELECT * FROM customers WHERE wdtt_password = ?", (wdtt_password,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_customers_without_sub_id() -> list[dict]:
    """Активные клиенты без sub_id (для миграции Subscription Proxy)."""
    async with conn() as db:
        cur = await db.execute(
            "SELECT * FROM customers WHERE wdtt_password IS NOT NULL "
            "AND (sub_id IS NULL OR sub_id = '')"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def set_customer_sub_id(telegram_id: int, sub_id: str) -> None:
    async with conn() as db:
        await db.execute(
            "UPDATE customers SET sub_id = ? WHERE telegram_id = ?",
            (sub_id, telegram_id),
        )


# ---------- trial ----------

async def has_used_trial(telegram_id: int) -> bool:
    async with conn() as db:
        cur = await db.execute("SELECT 1 FROM trials WHERE telegram_id = ?", (telegram_id,))
        return await cur.fetchone() is not None


async def claim_trial(telegram_id: int) -> bool:
    """Атомарно занять слот пробного периода.

    Возвращает True, если слот успешно занят и пробный период можно выдавать;
    False, если пользователь уже получал пробный период (занят раньше).
    """
    async with conn() as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO trials (telegram_id, used_at) VALUES (?, ?)",
            (telegram_id, int(time.time())),
        )
        await db.commit()
        return cur.rowcount > 0


async def release_trial(telegram_id: int) -> None:
    """Освободить слот (если выдача провалилась после claim)."""
    async with conn() as db:
        await db.execute("DELETE FROM trials WHERE telegram_id = ?", (telegram_id,))


async def mark_trial_used(telegram_id: int) -> None:
    async with conn() as db:
        await db.execute(
            "INSERT OR IGNORE INTO trials (telegram_id, used_at) VALUES (?, ?)",
            (telegram_id, int(time.time())),
        )


# ---------- referrals ----------

async def add_referral(referred_id: int, referrer_id: int) -> None:
    async with conn() as db:
        await db.execute(
            "INSERT OR IGNORE INTO referrals (referred_id, referrer_id, created_at, rewarded) "
            "VALUES (?, ?, ?, 0)",
            (referred_id, referrer_id, int(time.time())),
        )


async def claim_referral_reward(referred_id: int) -> int | None:
    """Возвращает referrer_id первого необработанного реферала (или None).

    Не помечает реферала вознаграждённым: пометка ставится mark_referral_rewarded
    только после фактического применения бонуса (иначе сбой применения
    привёл бы к потере бонуса навсегда).
    """
    async with conn() as db:
        cur = await db.execute(
            "SELECT referrer_id, rewarded FROM referrals WHERE referred_id = ?", (referred_id,)
        )
        row = await cur.fetchone()
        if not row or row["rewarded"]:
            return None
        return row["referrer_id"]


async def mark_referral_rewarded(referred_id: int) -> bool:
    """Атомарно помечает реферала вознаграждённым (только если ещё не помечен)."""
    async with conn() as db:
        cur = await db.execute(
            "UPDATE referrals SET rewarded = 1, rewarded_at = ? "
            "WHERE referred_id = ? AND rewarded = 0",
            (int(time.time()), referred_id),
        )
        return cur.rowcount > 0


async def get_referrals_count(referrer_id: int) -> int:
    async with conn() as db:
        cur = await db.execute(
            "SELECT COUNT(*) c FROM referrals WHERE referrer_id = ?", (referrer_id,)
        )
        row = await cur.fetchone()
        return int(row["c"]) if row else 0


async def get_total_referrals() -> int:
    async with conn() as db:
        cur = await db.execute("SELECT COUNT(*) c FROM referrals")
        row = await cur.fetchone()
        return int(row["c"]) if row else 0


async def get_site_user_by_email(email: str) -> dict | None:
    async with conn() as db:
        cur = await db.execute("SELECT * FROM site_users WHERE email = ?", (email,))
        row = await cur.fetchone()
        return dict(row) if row else None


# ---------- сайт-рефералы (отдельно от тг-рефералов) ----------

def generate_referral_code(length: int = 8) -> str:
    return secrets.token_urlsafe(length)[:length]


async def add_site_referral(referred_site_user_id: int, referrer_site_user_id: int) -> None:
    async with conn() as db:
        await db.execute(
            "INSERT OR IGNORE INTO site_referrals "
            "(referred_site_user_id, referrer_site_user_id, created_at, rewarded) "
            "VALUES (?, ?, ?, 0)",
            (referred_site_user_id, referrer_site_user_id, int(time.time())),
        )


async def get_site_referrals_count(site_user_id: int) -> int:
    async with conn() as db:
        cur = await db.execute(
            "SELECT COUNT(*) c FROM site_referrals WHERE referrer_site_user_id = ?",
            (site_user_id,),
        )
        row = await cur.fetchone()
        return int(row["c"]) if row else 0


async def get_site_user_by_referral_code(referral_code: str) -> dict | None:
    async with conn() as db:
        cur = await db.execute(
            "SELECT * FROM site_users WHERE referral_code = ?", (referral_code,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def bind_site_telegram(site_user_id: int, telegram_id: int) -> bool:
    """Привязывает site_user к telegram_id (ещё не привязанному)."""
    async with conn() as db:
        cur = await db.execute(
            "UPDATE site_users SET telegram_id = ? WHERE id = ? AND telegram_id IS NULL",
            (telegram_id, site_user_id),
        )
        return cur.rowcount > 0


# ---------- servers ----------

async def add_server(name: str, base_url: str, username: str, password: str,
                     verify_ssl: bool = False,
                     ssh_host: str | None = None, ssh_port: int | None = None,
                     ssh_user: str | None = None, ssh_password: str | None = None) -> int:
    async with conn() as db:
        cur = await db.execute(
            "INSERT INTO servers (name, base_url, username, password, verify_ssl, created_at,"
            " ssh_host, ssh_port, ssh_user, ssh_password) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, base_url, username, password, 1 if verify_ssl else 0, int(time.time()),
             ssh_host, ssh_port, ssh_user, ssh_password),
        )
        return int(cur.lastrowid or 0)


async def list_servers() -> list[dict]:
    async with conn() as db:
        cur = await db.execute("SELECT * FROM servers ORDER BY id")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_server(server_id: int) -> dict | None:
    async with conn() as db:
        cur = await db.execute("SELECT * FROM servers WHERE id = ?", (server_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def delete_server(server_id: int) -> None:
    async with conn() as db:
        await db.execute("DELETE FROM servers WHERE id = ?", (server_id,))


async def server_active_counts() -> dict[int, int]:
    now = int(time.time())
    async with conn() as db:
        cur = await db.execute(
            "SELECT server_id, COUNT(*) c FROM customers "
            "WHERE wdtt_password IS NOT NULL AND expires_at > ? AND server_id IS NOT NULL "
            "GROUP BY server_id",
            (now,),
        )
        rows = await cur.fetchall()
        return {row["server_id"]: int(row["c"]) for row in rows}


async def backfill_customer_servers(server_id: int) -> None:
    async with conn() as db:
        await db.execute(
            "UPDATE customers SET server_id = ? "
            "WHERE server_id IS NULL AND wdtt_password IS NOT NULL",
            (server_id,),
        )


# ---------- VK-аккаунты и учёт выданных хешей ----------

async def add_vk_account(label: str, cookies: str) -> int:
    async with conn() as db:
        cur = await db.execute(
            "INSERT INTO vk_accounts (label, cookies, active, created_at) VALUES (?, ?, 1, ?)",
            (label, cookies, int(time.time())),
        )
        return int(cur.lastrowid or 0)


async def list_vk_accounts(active_only: bool = True) -> list[dict]:
    async with conn() as db:
        q = "SELECT * FROM vk_accounts"
        if active_only:
            q += " WHERE active = 1"
        q += " ORDER BY id"
        rows = await (await db.execute(q)).fetchall()
        return [dict(r) for r in rows]


async def get_vk_account(account_id: int) -> dict | None:
    async with conn() as db:
        row = await (await db.execute("SELECT * FROM vk_accounts WHERE id = ?", (account_id,))).fetchone()
        return dict(row) if row else None


async def delete_vk_account(account_id: int) -> None:
    async with conn() as db:
        await db.execute("UPDATE vk_accounts SET active = 0 WHERE id = ?", (account_id,))


async def record_vk_issued(server_id: int, vk_account_id: int, user_password: str, vk_hash: str) -> None:
    async with conn() as db:
        await db.execute(
            "INSERT INTO vk_issued (server_id, vk_account_id, user_password, vk_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (server_id, vk_account_id, user_password, vk_hash, int(time.time())),
        )


async def vk_issued_counts(server_id: int | None = None) -> dict[int, int]:
    """Сколько хешей выдано каждым VK-аккаунтом (по серверу или всего)."""
    async with conn() as db:
        if server_id is None:
            rows = await (await db.execute(
                "SELECT vk_account_id, COUNT(*) c FROM vk_issued GROUP BY vk_account_id"
            )).fetchall()
        else:
            rows = await (await db.execute(
                "SELECT vk_account_id, COUNT(*) c FROM vk_issued WHERE server_id = ? GROUP BY vk_account_id",
                (server_id,),
            )).fetchall()
        return {int(r["vk_account_id"]): int(r["c"]) for r in rows}


async def vk_hashes_for_user(user_password: str) -> list[str]:
    async with conn() as db:
        rows = await (await db.execute(
            "SELECT vk_hash FROM vk_issued WHERE user_password = ? ORDER BY id", (user_password,)
        )).fetchall()
        return [r["vk_hash"] for r in rows]


# ---------- коды привязки сайта ----------

async def get_bind_code(code: str) -> dict | None:
    """Запись bind_codes по коду (для deep-link t.me/<bot>?start=bind_<code>)."""
    async with conn() as db:
        row = await (await db.execute(
            "SELECT * FROM bind_codes WHERE code = ? AND used = 0", (code,)
        )).fetchone()
        return dict(row) if row else None


async def mark_bind_code_used(code: str) -> None:
    async with conn() as db:
        await db.execute("UPDATE bind_codes SET used = 1 WHERE code = ?", (code,))


async def get_site_user_by_id(site_user_id: int) -> dict | None:
    async with conn() as db:
        row = await (await db.execute("SELECT * FROM site_users WHERE id = ?", (site_user_id,))).fetchone()
        return dict(row) if row else None


# ---------- вход на сайт через Telegram ----------

async def create_login_request(code: str) -> None:
    """Создаёт запрос на вход (код подтверждает бот)."""
    async with conn() as db:
        await db.execute(
            "INSERT OR REPLACE INTO login_requests (code, telegram_id, created_at, used) "
            "VALUES (?, NULL, ?, 0)",
            (code, int(time.time())),
        )


async def confirm_login_request(code: str, telegram_id: int, username: str | None = None) -> bool:
    """Бот подтверждает вход: записывает telegram_id (только один раз)."""
    async with conn() as db:
        cur = await db.execute(
            "UPDATE login_requests SET telegram_id = ?, username = COALESCE(?, username) "
            "WHERE code = ? AND telegram_id IS NULL AND used = 0",
            (telegram_id, username, code),
        )
        return cur.rowcount > 0


async def get_login_request(code: str) -> dict | None:
    async with conn() as db:
        row = await (await db.execute(
            "SELECT * FROM login_requests WHERE code = ?", (code,)
        )).fetchone()
        return dict(row) if row else None


async def mark_login_request_used(code: str) -> None:
    async with conn() as db:
        await db.execute("UPDATE login_requests SET used = 1 WHERE code = ?", (code,))


async def cleanup_login_requests(max_age: int = 3600) -> int:
    """Удаляет старые и использованные запросы на вход.

    Сканеры/боты могут дёргать /login/telegram тысячи раз — без очистки
    таблица login_requests растёт бесконечно.
    """
    cutoff = int(time.time()) - int(max_age)
    async with conn() as db:
        cur = await db.execute(
            "DELETE FROM login_requests WHERE created_at < ? OR used = 1", (cutoff,)
        )
        return cur.rowcount or 0


# ---------- plans ----------

async def list_plans() -> list[dict]:
    async with conn() as db:
        cur = await db.execute("SELECT * FROM plans ORDER BY sort_order ASC, id ASC")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_plan(plan_id: str) -> dict | None:
    async with conn() as db:
        cur = await db.execute("SELECT * FROM plans WHERE id = ?", (plan_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def upsert_plan(plan_id: str, name: str, days: int, price: float,
                      traffic_gb: int, max_devices: int) -> None:
    async with conn() as db:
        cur = await db.execute("SELECT sort_order FROM plans WHERE id = ?", (plan_id,))
        row = await cur.fetchone()
        if row is not None:
            sort_order = row["sort_order"]
        else:
            m = await (await db.execute("SELECT COALESCE(MAX(sort_order), 0) m FROM plans")).fetchone()
            sort_order = int(m["m"]) + 1
        await db.execute(
            "INSERT INTO plans (id, name, days, price, traffic_gb, max_devices, sort_order, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, days=excluded.days, price=excluded.price, "
            "traffic_gb=excluded.traffic_gb, max_devices=excluded.max_devices",
            (plan_id, name, days, price, traffic_gb, max_devices, sort_order, int(time.time())),
        )


async def delete_plan(plan_id: str) -> None:
    async with conn() as db:
        await db.execute("DELETE FROM plans WHERE id = ?", (plan_id,))


# ---------- stats ----------

async def stats() -> dict:
    now = int(time.time())
    async with conn() as db:
        customers = (await (await db.execute("SELECT COUNT(*) c FROM customers")).fetchone())["c"]
        active = (await (await db.execute(
            "SELECT COUNT(*) c FROM customers WHERE wdtt_password IS NOT NULL AND expires_at > ?",
            (now,),
        )).fetchone())["c"]
        paid = (await (await db.execute(
            "SELECT COUNT(*) c FROM payments WHERE status IN ('paid', 'fulfilled')"
        )).fetchone())["c"]
        revenue = (await (await db.execute(
            "SELECT SUM(CAST(amount AS REAL)) s FROM payments WHERE status IN ('paid', 'fulfilled')"
        )).fetchone())["s"]
    return {
        "customers": customers,
        "active": active,
        "paid": paid,
        "revenue": float(revenue or 0.0),
    }
