"""Print end / start / stranded recovery for build-plate swapper printers (spec 4.7, D5, D6).

Only a ``completed`` matched to the dispatched queue item takes a spare plate;
every other ending holds the queue (``swap.hold.printNotConfirmed``). The plate
count and hold are written in the same transaction as the queue item's status.

Drives the real ``on_print_complete`` / ``on_print_start`` / stranded sweep on
a real SQLite DB; MQTT, FTP, WebSocket and notifications are mocked.
"""

import types
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.bambu_ftp import DeleteResult
from backend.app.services.printer_manager import printer_manager
from backend.tests._fixtures.background_tasks import MAIN_TARGET, discarding_spawn_patch

pytestmark = pytest.mark.integration

DATA = {"filename": "job.gcode.3mf", "subtask_name": "job"}


@pytest.fixture
def maker(test_engine):
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
def app_on_test_db(maker):
    """Point every session the code under test opens at the fixture DB and mock the outside world."""
    with ExitStack() as stack:
        for target in ("backend.app.core.database.async_session", "backend.app.main.async_session"):
            stack.enter_context(patch(target, maker))
        for target in ("backend.app.main.ws_manager", "backend.app.main.mqtt_relay"):
            m = stack.enter_context(patch(target))
            for name in ("send_print_complete", "send_print_start", "broadcast", "on_print_complete", "on_print_start"):
                setattr(m, name, AsyncMock())
        stack.enter_context(patch("backend.app.main.notification_service", AsyncMock()))
        stack.enter_context(discarding_spawn_patch(MAIN_TARGET))
        stack.enter_context(patch("backend.app.main.clear_3mf_cache"))
        stack.enter_context(
            patch("backend.app.services.bambu_ftp.delete_file_async", AsyncMock(return_value=DeleteResult.DELETED))
        )
        stack.enter_context(patch("backend.app.main.notify_missing_spool_assignments_on_print_start", AsyncMock()))
        stack.enter_context(patch("backend.app.main._send_print_start_notification", AsyncMock()))
        yield
    printer_manager._awaiting_plate_clear.clear()


async def _setup(maker, *, swap=True, plates=2, item_status="printing", **printer_kw):
    async with maker() as db:
        p = Printer(
            name="A1",
            serial_number="SN-SWAP",
            ip_address="192.0.2.1",
            access_code="x",
            model="A1",
            auto_archive=False,
            swap_enabled=swap,
            swap_plates_remaining=plates,
            **printer_kw,
        )
        db.add(p)
        await db.flush()
        item = None
        if item_status:
            item = PrintQueueItem(printer_id=p.id, status=item_status)
            db.add(item)
        await db.commit()
        pid, iid = p.id, (item.id if item else None)
    printer_manager._swap_state = {pid: (swap, plates, None)}
    return pid, iid


async def _read(maker, pid, iid=None):
    async with maker() as db:
        p = await db.get(Printer, pid)
        item = await db.get(PrintQueueItem, iid) if iid else None
        return p.swap_plates_remaining, p.swap_hold_reason, (item.status if item else None)


async def _complete(pid, status):
    from backend.app.main import on_print_complete

    await on_print_complete(pid, {**DATA, "status": status})


async def test_completed_consumes_one_plate(maker):
    pid, iid = await _setup(maker, plates=2)
    await _complete(pid, "completed")
    assert await _read(maker, pid, iid) == (1, None, "completed")


async def test_completed_with_empty_holder_holds(maker):
    pid, iid = await _setup(maker, plates=0)
    await _complete(pid, "completed")
    assert await _read(maker, pid, iid) == (0, "swap.hold.outOfPlates", "completed")


async def test_duplicate_completion_consumes_once(maker):
    pid, iid = await _setup(maker, plates=2)
    await _complete(pid, "completed")
    await _complete(pid, "completed")
    remaining, _hold, status = await _read(maker, pid, iid)
    assert (remaining, status) == (1, "completed")


@pytest.mark.parametrize("status", ["failed", "aborted", "cancelled"])
async def test_failed_holds(maker, status):
    pid, iid = await _setup(maker, plates=2)
    await _complete(pid, status)
    remaining, hold, item_status = await _read(maker, pid, iid)
    assert (remaining, hold) == (2, "swap.hold.printNotConfirmed")
    assert item_status in ("failed", "cancelled")


async def test_unmatched_end_while_printing_is_ignored(maker):
    pid, iid = await _setup(maker, plates=2)
    with patch("backend.app.main._completion_belongs_to_queue_item", AsyncMock(return_value=False)):
        await _complete(pid, "completed")
    assert await _read(maker, pid, iid) == (2, None, "printing")


async def test_unmatched_end_without_printing_item_holds(maker):
    pid, _ = await _setup(maker, plates=2, item_status=None)
    await _complete(pid, "completed")
    assert await _read(maker, pid) == (2, "swap.hold.printNotConfirmed", None)


async def test_swap_disabled_printer_is_untouched_by_completion(maker):
    pid, iid = await _setup(maker, swap=False, plates=2)
    await _complete(pid, "completed")
    assert await _read(maker, pid, iid) == (2, None, "completed")


@pytest.mark.parametrize("swap,expected", [(True, False), (False, True)])
async def test_no_awaiting_plate_clear_for_swap_printer(maker, swap, expected):
    pid, _ = await _setup(maker, swap=swap)
    await _complete(pid, "completed")
    assert printer_manager.is_awaiting_plate_clear(pid) is expected


async def test_stranded_close_holds(maker):
    import backend.app.services.print_scheduler as scheduler_module
    from backend.app.services.print_scheduler import _STRANDED_PRINTING_GRACE_SECONDS, PrintScheduler

    pid, iid = await _setup(maker, plates=2)
    finished = types.SimpleNamespace(state="FINISH", connected=True)
    with (
        patch.object(scheduler_module, "async_session", maker),
        patch.object(scheduler_module.printer_manager, "get_status", lambda _pid: finished),
    ):
        scheduler = PrintScheduler()
        await scheduler._close_stranded_printing_items()
        scheduler._terminal_since[pid] -= _STRANDED_PRINTING_GRACE_SECONDS + 1
        await scheduler._close_stranded_printing_items()
    assert await _read(maker, pid, iid) == (2, "swap.hold.printNotConfirmed", "completed")


async def test_stranded_close_leaves_swap_disabled_printer_alone(maker):
    import backend.app.services.print_scheduler as scheduler_module
    from backend.app.services.print_scheduler import _STRANDED_PRINTING_GRACE_SECONDS, PrintScheduler

    pid, iid = await _setup(maker, swap=False, plates=2)
    finished = types.SimpleNamespace(state="FINISH", connected=True)
    with (
        patch.object(scheduler_module, "async_session", maker),
        patch.object(scheduler_module.printer_manager, "get_status", lambda _pid: finished),
    ):
        scheduler = PrintScheduler()
        await scheduler._close_stranded_printing_items()
        scheduler._terminal_since[pid] -= _STRANDED_PRINTING_GRACE_SECONDS + 1
        await scheduler._close_stranded_printing_items()
    assert await _read(maker, pid, iid) == (2, None, "completed")


async def _start(pid):
    from backend.app.main import on_print_start

    await on_print_start(pid, dict(DATA))


async def test_non_queue_print_start_holds(maker):
    pid, _ = await _setup(maker, plates=2, item_status=None)
    await _start(pid)
    assert await _read(maker, pid) == (2, "swap.hold.printNotConfirmed", None)


async def test_queue_print_start_does_not_hold(maker):
    pid, iid = await _setup(maker, plates=2)
    await _start(pid)
    assert await _read(maker, pid, iid) == (2, None, "printing")


@pytest.mark.parametrize("swap,expected_calls", [(True, 0), (False, 1)])
async def test_legacy_plate_check_skipped_for_swap(maker, swap, expected_calls):
    pid, _ = await _setup(maker, swap=swap, plates=2, plate_detection_enabled=True)
    check = AsyncMock(return_value=MagicMock(needs_calibration=False, is_empty=True))
    with (
        patch("backend.app.services.plate_detection.check_plate_empty", check),
        patch("backend.app.main.printer_manager.get_client", return_value=None),
    ):
        await _start(pid)
    assert check.await_count == expected_calls
