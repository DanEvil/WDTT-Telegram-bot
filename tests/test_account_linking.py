"""Tests for explicitly linking a website account to a Telegram account."""
import asyncio
import time

import db
import web_app


async def _insert_site_user(site_user_id: int, email: str = "user@example.com") -> None:
    async with db.conn() as conn:
        await conn.execute(
            "INSERT INTO site_users "
            "(id, email, username, password_hash, referral_code, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (site_user_id, email, "user", "hash", f"ref-{site_user_id}", int(time.time())),
        )


def test_link_migrates_site_data_and_refreshes_existing_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "accounts.db"))

    async def run():
        await db.init_db()
        await _insert_site_user(41)
        await db.save_site_session(
            "active-session",
            {
                "site_user_id": 41,
                "email": "user@example.com",
                "username": "user",
                "telegram_id": None,
                "created": int(time.time()),
            },
            int(time.time()) + 3600,
            100,
        )
        old_id = -41
        async with db.conn() as conn:
            await conn.execute(
                "INSERT INTO customers (telegram_id, wdtt_password, expires_at, plan_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (old_id, "vpn-pass", int(time.time()) + 86400, "month", int(time.time())),
            )
            await conn.execute(
                "INSERT INTO identities (provider, subject, user_id, created_at) "
                "VALUES ('email', 'user@example.com', ?, ?)",
                (old_id, int(time.time())),
            )
        await db.add_payment(old_id, "user", "site-payment", "month", "100.00")
        await db.set_payment_status("site-payment", "paid", paid_at=int(time.time()))
        await db.start_fulfillment("site-payment", old_id, "month", int(time.time()) + 86400)
        async with db.conn() as conn:
            await conn.execute(
                "INSERT INTO trials (telegram_id, used_at) VALUES (?, ?)",
                (old_id, int(time.time())),
            )

        assert await db.bind_site_telegram(41, 987654321) == "bound"
        session = await db.get_site_session("active-session")
        customer = await db.get_customer(987654321)
        payment = await db.get_payment("site-payment")
        fulfillment = await db.get_fulfillment("site-payment")
        email_identity = await db.get_identity("email", "user@example.com")
        telegram_identity = await db.get_identity("telegram", "987654321")
        async with db.conn() as conn:
            trial = await (await conn.execute(
                "SELECT telegram_id FROM trials WHERE telegram_id = ?", (987654321,)
            )).fetchone()
            site_user = await (await conn.execute(
                "SELECT telegram_id FROM site_users WHERE id = 41"
            )).fetchone()

        assert session["telegram_id"] == 987654321
        assert web_app._sub_id(session) == 987654321
        assert customer["wdtt_password"] == "vpn-pass"
        assert int(payment["telegram_id"]) == 987654321
        assert int(fulfillment["telegram_id"]) == 987654321
        assert trial and site_user["telegram_id"] == 987654321
        assert email_identity["user_id"] == telegram_identity["user_id"] == 987654321

    asyncio.run(run())


def test_link_refuses_to_merge_two_accounts_with_subscription_data(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "accounts.db"))

    async def run():
        await db.init_db()
        await _insert_site_user(42, "conflict@example.com")
        now = int(time.time())
        async with db.conn() as conn:
            await conn.execute(
                "INSERT INTO customers (telegram_id, wdtt_password, expires_at, plan_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (-42, "site-pass", now + 86400, "month", now),
            )
            await conn.execute(
                "INSERT INTO customers (telegram_id, wdtt_password, expires_at, plan_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (987654322, "telegram-pass", now + 172800, "quarter", now),
            )

        assert await db.bind_site_telegram(42, 987654322) == "data_conflict"
        site_user = await db.get_site_user_by_id(42)
        site_customer = await db.get_customer(-42)
        telegram_customer = await db.get_customer(987654322)
        assert site_user["telegram_id"] is None
        assert site_customer["wdtt_password"] == "site-pass"
        assert telegram_customer["wdtt_password"] == "telegram-pass"

    asyncio.run(run())


def test_link_migrates_existing_email_identity_to_telegram(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "accounts.db"))

    async def run():
        await db.init_db()
        email = "email-identity@example.com"
        await _insert_site_user(43, email)
        email_uid = 1_234_567_890
        now = int(time.time())
        async with db.conn() as conn:
            await conn.execute(
                "INSERT INTO customers (telegram_id, wdtt_password, expires_at, plan_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (email_uid, "email-pass", now + 86400, "month", now),
            )
            await conn.execute(
                "INSERT INTO identities (provider, subject, user_id, created_at) "
                "VALUES ('email', ?, ?, ?)",
                (email, email_uid, now),
            )

        assert await db.bind_site_telegram(43, 987654323) == "bound"
        assert (await db.get_customer(987654323))["wdtt_password"] == "email-pass"
        assert (await db.get_identity("email", email))["user_id"] == 987654323

    asyncio.run(run())
