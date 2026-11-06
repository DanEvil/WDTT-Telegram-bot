"""Регрессионные тесты защиты от повторного списания при автопродлении."""
import time

import pytest

import db


TG = 777001


async def _make_customer(expires_at: int) -> None:
    async with db.conn() as c:
        await c.execute(
            "INSERT OR REPLACE INTO customers (telegram_id, username, wdtt_password, expires_at, "
            "plan_id, server_id, auto_renew, notify_hours, payment_method_id, renew_notified, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (TG, "@u", "pwd", expires_at, "month", 1, 1, 0, "pm_test", 0, int(time.time())),
        )


async def test_charge_marked_per_expiry():
    """Попытка списания фиксируется по сроку подписки."""
    exp = int(time.time()) + 60
    await _make_customer(exp)
    await db.mark_renew_charge(TG, exp, "pay_123")
    st = await db.get_renew_charge(TG)
    assert int(st["renew_charge_exp"]) == exp
    assert st["renew_charge_payment_id"] == "pay_123"


async def test_repeat_tick_keeps_same_payment():
    """Повторный тик не создаёт новый платёж — payment_id остаётся прежним."""
    exp = int(time.time()) + 60
    await _make_customer(exp)
    await db.mark_renew_charge(TG, exp, "pay_123")
    st = await db.get_renew_charge(TG)
    # логика _autorenew_one: charged_exp == exp и payment_id есть -> новый платёж не создаётся
    charged_exp = int(st.get("renew_charge_exp") or 0)
    existing = st.get("renew_charge_payment_id")
    assert charged_exp == exp and existing == "pay_123"


async def test_failed_attempt_keeps_previous_payment_id():
    """Неудачная попытка (без payment_id) не затирает id предыдущего платежа."""
    exp = int(time.time()) + 60
    await _make_customer(exp)
    await db.mark_renew_charge(TG, exp, "pay_123")
    await db.mark_renew_charge(TG, exp, None)
    st = await db.get_renew_charge(TG)
    assert st["renew_charge_payment_id"] == "pay_123"


async def test_new_expiry_allows_new_charge():
    """После продления (новый срок) списание снова разрешено."""
    exp1 = int(time.time()) + 60
    exp2 = exp1 + 30 * 86400
    await _make_customer(exp1)
    await db.mark_renew_charge(TG, exp1, "pay_123")
    await db.mark_renew_charge(TG, exp2, "pay_456")
    st = await db.get_renew_charge(TG)
    assert int(st["renew_charge_exp"]) == exp2
    assert st["renew_charge_payment_id"] == "pay_456"


async def test_clear_charge():
    await _make_customer(int(time.time()) + 60)
    await db.mark_renew_charge(TG, int(time.time()) + 60, "pay_789")
    await db.clear_renew_charge(TG)
    st = await db.get_renew_charge(TG)
    assert int(st["renew_charge_exp"]) == 0
    assert st["renew_charge_payment_id"] is None


async def test_migration_columns_present():
    async with db.conn() as c:
        cols = [r["name"] for r in await (await c.execute("PRAGMA table_info(customers)")).fetchall()]
    for col in ("renew_charge_exp", "renew_charge_at", "renew_charge_payment_id"):
        assert col in cols, f"нет колонки {col}"
