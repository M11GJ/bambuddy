"""Swap (SwapMod) state: DB is the source of truth, memory mirrors it after commit,
and anything unknown blocks dispatch (fail closed)."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.printer import Printer
from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.printer_manager import printer_manager, printer_state_to_dict


@pytest.fixture(autouse=True)
def app_sessions_on_test_engine(test_engine):
    """printer_manager opens its own sessions; point them at the fixture DB."""
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("backend.app.core.database.async_session", maker):
        yield
    printer_manager._swap_state = {}
    printer_manager._swap_state_loaded = False


async def _printer(db, n=1, **kw):
    p = Printer(name="A1", serial_number=f"SN{n}", ip_address="192.0.2.1", access_code="x", model="A1", **kw)
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


async def test_disabled_printer_is_never_blocked(db_session):
    p = await _printer(db_session, swap_enabled=False)
    assert await printer_manager.load_swap_state_from_db() is True
    assert printer_manager.is_swap_blocked(p.id) is None


async def test_enabled_printer_without_plate_count_is_blocked(db_session):
    p = await _printer(db_session, n=2, swap_enabled=True, swap_plates_remaining=None)
    await printer_manager.load_swap_state_from_db()
    assert printer_manager.is_swap_blocked(p.id) == "swap.hold.platesUnknown"


async def test_not_loaded_blocks_every_printer(db_session):
    p = await _printer(db_session, n=3, swap_enabled=False)
    printer_manager._swap_state_loaded = False
    assert printer_manager.is_swap_blocked(p.id) == "swap.hold.stateUnavailable"
    await printer_manager.load_swap_state_from_db()
    assert printer_manager.is_swap_blocked(p.id) is None


async def test_failed_load_blocks_every_printer(db_session):
    p = await _printer(db_session, n=4, swap_enabled=False)
    await printer_manager.load_swap_state_from_db()
    with patch("backend.app.core.database.async_session", side_effect=RuntimeError("db down")):
        assert await printer_manager.load_swap_state_from_db() is False
    assert printer_manager.is_swap_blocked(p.id) == "swap.hold.stateUnavailable"


async def test_failed_refresh_blocks_every_printer(db_session):
    p = await _printer(db_session, n=5, swap_enabled=False)
    await printer_manager.load_swap_state_from_db()
    with (
        patch("backend.app.core.database.async_session", side_effect=RuntimeError("db down")),
        patch.object(printer_manager, "start_swap_state_reload") as reload,
    ):
        await printer_manager._refresh_swap_state(p.id)
    assert printer_manager.is_swap_blocked(p.id) == "swap.hold.stateUnavailable"
    reload.assert_called_once()


async def test_consume_never_goes_below_zero(db_session):
    p = await _printer(db_session, n=6, swap_enabled=True, swap_plates_remaining=1)
    assert await printer_manager.consume_swap_plate(db_session, p.id) == 0
    assert await printer_manager.consume_swap_plate(db_session, p.id) is None
    await db_session.commit()
    row = (await db_session.execute(select(Printer.swap_plates_remaining).where(Printer.id == p.id))).scalar_one()
    assert row == 0


async def test_set_plates_and_hold_are_visible_only_after_commit_and_refresh(db_session):
    p = await _printer(db_session, n=7, swap_enabled=True, swap_plates_remaining=None)
    await printer_manager.load_swap_state_from_db()
    await printer_manager.set_swap_plates(db_session, p.id, 3)
    await db_session.commit()
    assert printer_manager.is_swap_blocked(p.id) == "swap.hold.platesUnknown"  # memory not refreshed yet
    await printer_manager._refresh_swap_state(p.id)
    assert printer_manager.is_swap_blocked(p.id) is None

    with patch("backend.app.services.notification_service.notification_service.on_queue_job_waiting") as notify:
        await printer_manager.set_swap_hold(db_session, p.id, "swap.hold.outOfPlates", "[swap.x] Holder empty.")
        assert printer_manager.is_swap_blocked(p.id) is None  # not committed/applied yet
        await db_session.commit()
        await printer_manager._refresh_swap_state(p.id)
        assert printer_manager.is_swap_blocked(p.id) == "swap.hold.outOfPlates"
        # A second refresh with the same hold must not notify again (edge-triggered).
        await printer_manager._refresh_swap_state(p.id)
    notify.assert_awaited_once()
    reason = notify.await_args.kwargs["waiting_reason"]
    assert "[" not in reason and "swap.hold" not in reason
    assert "Holder empty." in reason

    await printer_manager.clear_swap_hold(db_session, p.id)
    await db_session.commit()
    await printer_manager._refresh_swap_state(p.id)
    assert printer_manager.is_swap_blocked(p.id) is None


async def test_apply_without_running_loop_is_a_no_op():
    printer_manager.apply_swap_state_after_commit(1)  # must not raise or leak a coroutine


async def test_apply_refreshes_memory_on_the_running_loop(db_session):
    p = await _printer(db_session, n=10, swap_enabled=True, swap_plates_remaining=1)
    await printer_manager.load_swap_state_from_db()
    await printer_manager.set_swap_plates(db_session, p.id, 0)
    await printer_manager.set_swap_hold(db_session, p.id, "swap.hold.outOfPlates", None)
    await db_session.commit()
    with (
        patch.object(printer_manager, "_loop", asyncio.get_running_loop()),
        patch("backend.app.services.notification_service.notification_service.on_queue_job_waiting"),
    ):
        printer_manager.apply_swap_state_after_commit(p.id)
        for _ in range(50):
            if printer_manager.is_swap_blocked(p.id):
                break
            await asyncio.sleep(0.01)
    assert printer_manager.is_swap_blocked(p.id) == "swap.hold.outOfPlates"


async def test_state_dict_exposes_swap_state(db_session):
    p = await _printer(db_session, n=8, swap_enabled=True, swap_plates_remaining=2)
    printer_manager._swap_state_loaded = False  # before the startup load
    assert printer_state_to_dict(PrinterState(), p.id)["swap"]["hold_reason"] == "swap.hold.stateUnavailable"
    await printer_manager.load_swap_state_from_db()
    assert printer_state_to_dict(PrinterState(), p.id)["swap"] == {
        "enabled": True,
        "plates_remaining": 2,
        "hold_reason": None,
    }


async def test_hold_notification_survives_notifier_errors(db_session):
    p = await _printer(db_session, n=9, swap_enabled=True, swap_plates_remaining=0)
    await printer_manager.load_swap_state_from_db()
    await printer_manager.set_swap_hold(db_session, p.id, "swap.hold.outOfPlates", None)
    await db_session.commit()
    with patch(
        "backend.app.services.notification_service.notification_service.on_queue_job_waiting",
        AsyncMock(side_effect=RuntimeError("smtp down")),
    ):
        await printer_manager._refresh_swap_state(p.id)
    assert printer_manager.is_swap_blocked(p.id) == "swap.hold.outOfPlates"
