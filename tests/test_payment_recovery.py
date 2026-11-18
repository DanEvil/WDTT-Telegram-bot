"""Restart recovery tests for payments already confirmed by YooKassa."""
import asyncio
import time

import db


def test_paid_payment_and_incomplete_fulfillment_survive_restart(tmp_path, monkeypatch):
    db_path = str(tmp_path / "payments.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)

    async def run():
        await db.init_db()
        await db.add_payment(123, "buyer", "paid-1", "month", "100.00")
        await db.set_payment_status("paid-1", "paid", paid_at=int(time.time()))
        await db.start_fulfillment("paid-1", 123, "month", int(time.time()) + 30 * 86400)

        # A paid payment stays eligible even after the abandoned-cart cutoff.
        async with db.conn() as conn:
            await conn.execute(
                "UPDATE payments SET created_at = ? WHERE payment_id = ?",
                (int(time.time()) - 8 * 86400, "paid-1"),
            )

        # Startup resets an interrupted journal entry; the poller will retry it.
        assert await db.reset_stale_fulfillments() == 1
        pending = await db.get_pending_payments()
        fulfillment = await db.get_fulfillment("paid-1")
        return pending, fulfillment

    pending, fulfillment = asyncio.run(run())
    assert [payment["payment_id"] for payment in pending] == ["paid-1"]
    assert fulfillment["status"] == "pending"
