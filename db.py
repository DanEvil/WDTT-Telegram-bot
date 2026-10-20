"""Хранение данных: SQLite через aiosqlite."""
import time
from contextlib import asynccontextmanager

import aiosqlite

from config import DB_PATH


@asynccontextmanager
async def conn():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        yield db
        await db.commit()
    finally:
        await db.close()


async def init_db() -> None:
    async with conn() as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS customers (
                telegram_id  INTEGER PRIMARY KEY,
                username     TEXT,
                wdtt_password TEXT,
                expires_at   INTEGER NOT NULL DEFAULT 0,
                plan_id      TEXT,
                server_id    INTEGER,
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
                          username: str | None = None) -> None:
    async with conn() as db:
        await db.execute(
            """
            INSERT INTO customers (telegram_id, username, wdtt_password, expires_at, plan_id, server_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username      = COALESCE(excluded.username, customers.username),
                wdtt_password = excluded.wdtt_password,
                expires_at    = excluded.expires_at,
                plan_id       = excluded.plan_id,
                server_id     = excluded.server_id
            """,
            (telegram_id, username, wdtt_password, expires_at, plan_id, server_id, int(time.time())),
        )


async def clear_customer(telegram_id: int) -> None:
    async with conn() as db:
        await db.execute(
            "UPDATE customers SET wdtt_password = NULL, expires_at = 0, plan_id = NULL WHERE telegram_id = ?",
            (telegram_id,),
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


async def get_all_customers() -> list[dict]:
    async with conn() as db:
        cur = await db.execute("SELECT * FROM customers ORDER BY created_at DESC")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


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


# ---------- servers ----------

async def add_server(name: str, base_url: str, username: str, password: str,
                     verify_ssl: bool = False) -> int:
    async with conn() as db:
        cur = await db.execute(
            "INSERT INTO servers (name, base_url, username, password, verify_ssl, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, base_url, username, password, 1 if verify_ssl else 0, int(time.time())),
        )
        return cur.lastrowid


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
