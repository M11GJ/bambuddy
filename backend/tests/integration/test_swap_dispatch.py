"""Dispatch gating for build-plate swapper printers (spec 4.3 / 4.4).

Drives ``PrintScheduler._start_print`` on a real in-memory DB with the same
harness shape as the nozzle-mismatch and cleanup-library tests: MQTT, FTP and
notifications are mocked, everything the gate decides on is real.
"""

import json
import zipfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.models  # noqa: F401 - populate Base.metadata
import backend.app.services.print_scheduler as scheduler_module
from backend.app.core.database import Base
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.print_scheduler import PrintScheduler, _ModelCandidate
from backend.app.services.printer_manager import printer_manager
from backend.app.services.swap_camera_gate import CameraGateResult
from backend.tests._fixtures.background_tasks import discarding_spawn_patch

MARK = "; --- AUTO-CLEAR START ---"
GOOD_GCODE = f"G28\n{MARK}\nG1 Y265\n; EXECUTABLE_BLOCK_END\n"
NO_MARKER_GCODE = "G28\n; EXECUTABLE_BLOCK_END\n"


def _write_3mf(path: Path, gcode: str) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/plate_1.gcode", gcode)
        bbox = [118, 118, 138, 138]
        z.writestr(
            "Metadata/plate_1.json", json.dumps({"bbox_all": bbox, "bbox_objects": [{"name": "c", "bbox": bbox}]})
        )


# tests/conftest.py starts each test with the swap state loaded and empty.


@pytest.fixture
async def case_factory(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    counter = 0

    async def make(*, swap_enabled=True, plates=3, hold=None, gcode=GOOD_GCODE, injection=False, memory=None):
        nonlocal counter
        counter += 1
        base_dir = tmp_path / f"case-{counter}"
        source = base_dir / "library" / "job.gcode.3mf"
        source.parent.mkdir(parents=True)
        _write_3mf(source, gcode)
        async with session_maker() as db:
            printer = Printer(
                name="A1",
                serial_number=f"SN-{counter}",
                ip_address="127.0.0.1",
                access_code="ac",
                model="A1",
                swap_enabled=swap_enabled,
                swap_plates_remaining=plates,
                swap_hold_reason=hold,
            )
            lib = LibraryFile(
                filename="job.gcode.3mf",
                file_path=str(source),
                file_type="3mf",
                file_size=source.stat().st_size,
            )
            db.add_all([printer, lib])
            await db.flush()
            item = PrintQueueItem(
                printer_id=printer.id,
                library_file_id=lib.id,
                status="pending",
                cleanup_library_after_dispatch=True,
                gcode_injection=injection,
                bed_levelling="on",
                flow_cali="off",
                vibration_cali=True,
                layer_inspect=False,
                timelapse=False,
                use_ams=True,
                nozzle_offset_cali="on",
            )
            db.add(item)
            await db.commit()
        # The in-memory mirror; tests that exercise the DB re-read override it.
        printer_manager._swap_state[printer.id] = memory or (swap_enabled, plates, hold)
        return SimpleNamespace(
            session_maker=session_maker,
            base_dir=base_dir,
            source=source,
            printer_id=printer.id,
            library_file_id=lib.id,
            item_id=item.id,
            upload=AsyncMock(return_value=True),
            start_print=MagicMock(return_value=True),
            archive_print=AsyncMock(),
            preheat=AsyncMock(return_value=True),
            on_failed=AsyncMock(),
            on_plate_not_empty=AsyncMock(),
            apply_after_commit=MagicMock(),
        )

    try:
        yield make
    finally:
        await engine.dispose()


async def _run(ctx, gate: CameraGateResult | None = None):
    scheduler = PrintScheduler()
    gate_mock = AsyncMock(return_value=gate or CameraGateResult("pass", clip_percent=10.0, diff_percent=0.1))

    async def archive_print(self, *, printer_id, source_file, original_filename, **kw):
        await ctx.archive_print(source_file)
        rel = Path("archives") / "a.gcode.3mf"
        (ctx.base_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (ctx.base_dir / rel).write_bytes(Path(source_file).read_bytes())
        archive = PrintArchive(
            printer_id=printer_id,
            filename=original_filename,
            file_path=str(rel),
            file_size=1,
            status="completed",
            library_file_id=kw.get("library_file_id"),
        )
        self.db.add(archive)
        await self.db.flush()
        return archive

    patches = [
        patch.object(scheduler_module.settings, "base_dir", ctx.base_dir),
        patch.object(scheduler_module.settings, "archive_dir", ctx.base_dir / "archive"),
        patch("backend.app.services.archive.ArchiveService.archive_print", new=archive_print),
        patch("backend.app.services.print_scheduler.printer_manager.is_connected", MagicMock(return_value=True)),
        patch("backend.app.services.print_scheduler.printer_manager.get_status", MagicMock(return_value=None)),
        patch("backend.app.services.print_scheduler.printer_manager.start_print", ctx.start_print),
        patch("backend.app.services.print_scheduler.printer_manager.set_awaiting_plate_clear", MagicMock()),
        patch(
            "backend.app.services.print_scheduler.printer_manager.apply_swap_state_after_commit",
            ctx.apply_after_commit,
        ),
        patch("backend.app.services.print_scheduler.run_camera_gate", gate_mock),
        patch(
            "backend.app.services.print_scheduler.get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0, 1.0))
        ),
        patch("backend.app.services.print_scheduler.delete_file_async", AsyncMock(return_value=True)),
        patch("backend.app.services.print_scheduler.upload_file_async", ctx.upload),
        patch("backend.app.services.print_scheduler.cache_3mf_download", MagicMock()),
        discarding_spawn_patch(),
        patch("backend.app.services.notification_service.notification_service.on_queue_job_started", AsyncMock()),
        patch("backend.app.services.notification_service.notification_service.on_queue_job_failed", ctx.on_failed),
        patch(
            "backend.app.services.notification_service.notification_service.on_plate_not_empty",
            ctx.on_plate_not_empty,
        ),
        patch("backend.app.services.mqtt_relay.mqtt_relay.on_queue_job_started", AsyncMock()),
        patch("backend.app.services.print_scheduler.ws_manager.send_queue_item_failed", AsyncMock()),
        patch.object(scheduler, "_preheat_and_soak", ctx.preheat),
        patch.object(scheduler, "_propagate_owner_to_printer_manager", AsyncMock()),
        patch.object(scheduler, "_power_off_if_needed", AsyncMock()),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        async with ctx.session_maker() as db:
            item = await db.get(PrintQueueItem, ctx.item_id)
            await scheduler._start_print(db, item)
    async with ctx.session_maker() as db:
        return (
            await db.get(PrintQueueItem, ctx.item_id),
            await db.get(Printer, ctx.printer_id),
            await db.get(LibraryFile, ctx.library_file_id),
            gate_mock,
        )


def _idle(state="IDLE"):
    return [
        patch("backend.app.services.print_scheduler.printer_manager.is_connected", MagicMock(return_value=True)),
        patch(
            "backend.app.services.print_scheduler.printer_manager.get_status",
            MagicMock(return_value=SimpleNamespace(state=state)),
        ),
        patch(
            "backend.app.services.print_scheduler.printer_manager.is_awaiting_plate_clear",
            MagicMock(return_value=False),
        ),
    ]


def test_blocked_printer_is_not_idle():
    printer_manager._swap_state[901] = (True, 3, "swap.hold.outOfPlates")
    scheduler = PrintScheduler()
    with ExitStack() as stack:
        for p in _idle():
            stack.enter_context(p)
        assert scheduler._is_printer_idle(901) is False
        # Drying is not gated by the swapper.
        assert scheduler._is_printer_idle(901, for_dispatch=False) is True


def test_disabled_printer_unchanged():
    printer_manager._swap_state[902] = (False, None, None)
    scheduler = PrintScheduler()
    with ExitStack() as stack:
        for p in _idle():
            stack.enter_context(p)
        assert scheduler._is_printer_idle(902) is True
        assert scheduler._pinned_hold_reason(902, "P1S", True) == "Busy: P1S"


def test_pinned_hold_reason_names_swap_hold():
    printer_manager._swap_state[903] = (True, 0, "swap.hold.outOfPlates")
    assert PrintScheduler._pinned_hold_reason(903, "A1", True).startswith("[swap.hold.outOfPlates]")


async def test_injection_rejected(case_factory):
    ctx = await case_factory(injection=True)
    item, _, _, gate = await _run(ctx)
    assert item.status == "failed"
    assert item.error_message.startswith("[swap.guard.injectionNotAllowed]")
    ctx.upload.assert_not_called()
    gate.assert_not_called()
    assert not ctx.on_failed.call_args.kwargs["reason"].startswith("[")


async def test_db_hold_rechecked(case_factory):
    # Memory says "go", the DB says "held": the DB wins.
    ctx = await case_factory(hold="swap.hold.outOfPlates", memory=(True, 3, None))
    item, printer, lib, gate = await _run(ctx)
    assert item.status == "pending"
    ctx.upload.assert_not_called()
    gate.assert_not_called()
    # No side effects before the gate: the library file is not consumed.
    ctx.archive_print.assert_not_called()
    assert lib is not None and item.library_file_id == ctx.library_file_id
    ctx.apply_after_commit.assert_called_with(ctx.printer_id)


async def test_db_unknown_plates_rechecked(case_factory):
    ctx = await case_factory(plates=None, memory=(True, 3, None))
    item, _, _, gate = await _run(ctx)
    assert item.status == "pending"
    gate.assert_not_called()
    ctx.upload.assert_not_called()


async def test_invalid_config_holds_printer(case_factory):
    ctx = await case_factory()
    async with ctx.session_maker() as db:
        p = await db.get(Printer, ctx.printer_id)
        p.swap_config = "{not json"
        await db.commit()
    item, printer, _, gate = await _run(ctx)
    assert item.status == "pending"
    assert printer.swap_hold_reason == "swap.hold.configInvalid"
    gate.assert_not_called()
    ctx.upload.assert_not_called()
    ctx.apply_after_commit.assert_called_with(ctx.printer_id)


async def test_camera_fail_holds_printer(case_factory):
    ctx = await case_factory()
    import cv2
    import numpy as np

    frame = cv2.imencode(".jpg", np.full((1000, 2000, 3), 90, np.uint8))[1].tobytes()
    item, printer, lib, _ = await _run(
        ctx, CameraGateResult("fail", "swap.gate.noPlate", clip_percent=0.5, diff_percent=None, image=frame)
    )
    assert printer.swap_hold_reason == "swap.gate.noPlate"
    assert item.status == "pending"
    ctx.upload.assert_not_called()
    ctx.preheat.assert_not_called()
    ctx.archive_print.assert_not_called()
    assert lib is not None
    ctx.apply_after_commit.assert_called_with(ctx.printer_id)
    ctx.on_plate_not_empty.assert_awaited_once()
    sent = cv2.imdecode(np.frombuffer(ctx.on_plate_not_empty.call_args.kwargs["image_data"], np.uint8), 1)
    assert max(sent.shape[:2]) == 768  # the plate crop, not the 2000px frame


async def test_camera_busy_defers_without_hold(case_factory):
    ctx = await case_factory()
    item, printer, _, _ = await _run(ctx, CameraGateResult("busy", "swap.gate.cameraBusy"))
    assert item.status == "pending"
    assert printer.swap_hold_reason is None
    assert "swap.gate.cameraBusy" in item.waiting_reason
    ctx.upload.assert_not_called()
    ctx.archive_print.assert_not_called()


async def test_ai_pending_defers_without_hold(case_factory):
    ctx = await case_factory()
    item, printer, lib, gate = await _run(ctx, CameraGateResult("pending", "swap.gate.aiPending", clip_percent=9.0))
    assert item.status == "pending"
    assert printer.swap_hold_reason is None
    assert item.waiting_reason.startswith("[swap.gate.aiPending]")
    ctx.upload.assert_not_called()
    ctx.archive_print.assert_not_called()
    ctx.on_plate_not_empty.assert_not_called()
    assert lib is not None
    # The scheduler hands the global AI settings (defaults here) to the gate.
    assert gate.call_args.kwargs["ai"].mode == "shadow"
    assert gate.call_args.kwargs["item_id"] == ctx.item_id


async def test_pass_after_ai_pending_clears_waiting_reason(case_factory):
    ctx = await case_factory()
    await _run(ctx, CameraGateResult("pending", "swap.gate.aiPending"))
    item, _, _, _ = await _run(ctx)
    assert item.status == "printing"
    assert item.waiting_reason is None


async def test_guard_fail_fails_item_only(case_factory):
    ctx = await case_factory(gcode=NO_MARKER_GCODE)
    item, printer, _, _ = await _run(ctx)
    assert item.status == "failed"
    assert item.error_message.startswith("[swap.guard.noMarker]")
    assert printer.swap_hold_reason is None
    ctx.upload.assert_not_called()
    ctx.preheat.assert_not_called()


async def test_external_notification_has_no_key_prefix(case_factory):
    ctx = await case_factory(gcode=NO_MARKER_GCODE)
    await _run(ctx)
    reason = ctx.on_failed.call_args.kwargs["reason"]
    assert reason and not reason.startswith("[")


async def test_happy_path_uploads_without_preheat(case_factory):
    ctx = await case_factory()
    item, _, _, gate = await _run(ctx)
    gate.assert_awaited_once()
    ctx.upload.assert_called()
    ctx.start_print.assert_called_once()
    ctx.preheat.assert_not_called()
    assert item.status == "printing"


async def test_disabled_printer_dispatch_unchanged(case_factory):
    ctx = await case_factory(swap_enabled=False, plates=None, gcode=NO_MARKER_GCODE, injection=False)
    item, _, _, gate = await _run(ctx)
    gate.assert_not_called()
    ctx.preheat.assert_awaited_once()
    ctx.start_print.assert_called_once()
    assert item.status == "printing"


async def test_wake_skips_blocked_printer(case_factory):
    ctx = await case_factory(hold="swap.hold.outOfPlates")
    scheduler = PrintScheduler()
    candidate = _ModelCandidate(
        target_model="A1", sliced_for=None, required_filament_types=None, filament_overrides=None
    )
    async with ctx.session_maker() as db:
        printer = await db.get(Printer, ctx.printer_id)
        with (
            patch.object(scheduler, "_printers_for_model", AsyncMock(return_value=[printer])),
            patch.object(scheduler, "_get_smart_plugs", AsyncMock(return_value=[])) as plugs,
            patch("backend.app.services.print_scheduler.printer_manager.is_connected", MagicMock(return_value=False)),
        ):
            result = await scheduler._wake_printer_for_model(db, [candidate], None, set(), {printer.id}, False)
    assert result == (None, None)
    plugs.assert_not_called()


async def test_keep_warm_skips_swap_printer():
    printer_manager._swap_state[904] = (True, 3, None)
    printer_manager._swap_state[905] = (False, None, None)
    scheduler = PrintScheduler()
    items = [SimpleNamespace(id=1, printer_id=904), SimpleNamespace(id=2, printer_id=905)]
    with (
        patch.object(scheduler, "_get_bool_setting", AsyncMock(return_value=True)),
        patch.object(scheduler, "_get_int_setting", AsyncMock(return_value=90)),
        patch.object(scheduler, "_sweep_keep_warm") as sweep,
        patch("backend.app.services.print_scheduler.printer_manager.get_status", MagicMock(return_value=None)),
    ):
        await scheduler._apply_keep_warm(MagicMock(), items, [], {904, 905}, True)
    active = sweep.call_args.kwargs["active_candidates"]
    assert 904 not in active
    assert 905 in active


async def test_hold_during_upload_stops_the_start(case_factory):
    # A hold that lands while the file uploads must still stop the start command.
    ctx = await case_factory()

    async def upload_then_hold(*_a, **_kw):
        printer_manager._swap_state[ctx.printer_id] = (True, 3, "swap.hold.printNotConfirmed")
        return True

    ctx.upload.side_effect = upload_then_hold
    item, _, _, _ = await _run(ctx)
    ctx.upload.assert_called()
    ctx.start_print.assert_not_called()
    assert item.status == "pending"
    assert item.started_at is None


async def test_keep_warm_fails_closed_before_swap_state_loads():
    printer_manager._swap_state_loaded = False
    scheduler = PrintScheduler()
    items = [SimpleNamespace(id=2, printer_id=905)]
    with (
        patch.object(scheduler, "_get_bool_setting", AsyncMock(return_value=True)),
        patch.object(scheduler, "_get_int_setting", AsyncMock(return_value=90)),
        patch.object(scheduler, "_sweep_keep_warm") as sweep,
        patch("backend.app.services.print_scheduler.printer_manager.get_status", MagicMock(return_value=None)),
    ):
        await scheduler._apply_keep_warm(MagicMock(), items, [], {905}, True)
    assert sweep.call_args.kwargs["active_candidates"] == set()


async def test_enable_during_upload_stops_ungated_start(case_factory):
    # Dispatch started on a normal printer (no gate ran); swap was enabled while
    # the file uploaded. The mirror now says swap, so the start must not go out.
    ctx = await case_factory(swap_enabled=False, plates=None, gcode=NO_MARKER_GCODE)

    async def upload_then_enable(*_a, **_kw):
        printer_manager._swap_state[ctx.printer_id] = (True, 3, None)
        return True

    ctx.upload.side_effect = upload_then_enable
    item, _, _, _ = await _run(ctx)
    ctx.upload.assert_called()
    ctx.start_print.assert_not_called()
    assert item.status == "pending"


async def test_put_enable_during_upload_blocks_via_mirror(case_factory):
    # PUT-enable resets the plate count, so the mirror reports platesUnknown.
    ctx = await case_factory(swap_enabled=False, plates=None, gcode=NO_MARKER_GCODE)

    async def upload_then_enable(*_a, **_kw):
        printer_manager._swap_state[ctx.printer_id] = (True, None, None)
        assert printer_manager.is_swap_blocked(ctx.printer_id) == "swap.hold.platesUnknown"
        return True

    ctx.upload.side_effect = upload_then_enable
    item, _, _, _ = await _run(ctx)
    ctx.start_print.assert_not_called()
    assert item.status == "pending"


async def _set_override(ctx, value="swap.gate.aiObject"):
    async with ctx.session_maker() as db:
        await db.execute(update(Printer).where(Printer.id == ctx.printer_id).values(swap_ai_override=value))
        await db.commit()


async def test_override_skips_ai_once_and_is_consumed_on_dispatch(case_factory):
    ctx = await case_factory()
    await _set_override(ctx)
    item, printer, _, gate = await _run(ctx)
    assert gate.call_args.kwargs["ai_override"] is True
    assert item.status == "printing"
    assert printer.swap_ai_override is None
    # Next dispatch: the AI runs again.
    async with ctx.session_maker() as db:
        await db.execute(update(PrintQueueItem).where(PrintQueueItem.id == ctx.item_id).values(status="pending"))
        await db.commit()
    _, _, _, gate2 = await _run(ctx)
    assert gate2.call_args.kwargs["ai_override"] is False


async def test_override_kept_while_gate_not_passed(case_factory):
    ctx = await case_factory()
    await _set_override(ctx)
    _, printer, _, _ = await _run(ctx, CameraGateResult("busy", "swap.gate.cameraBusy"))
    assert printer.swap_ai_override == "swap.gate.aiObject"


async def test_override_cleared_when_clip_fails(case_factory):
    ctx = await case_factory()
    await _set_override(ctx)
    _, printer, _, _ = await _run(ctx, CameraGateResult("fail", "swap.gate.noPlate", clip_percent=0.5))
    assert printer.swap_hold_reason == "swap.gate.noPlate"
    assert printer.swap_ai_override is None


async def test_override_consumed_even_if_guard_fails_afterwards(case_factory):
    ctx = await case_factory(gcode=NO_MARKER_GCODE)
    await _set_override(ctx)
    item, printer, _, gate = await _run(ctx)
    assert gate.call_args.kwargs["ai_override"] is True
    assert item.status == "failed"
    assert printer.swap_ai_override is None
    async with ctx.session_maker() as db:
        await db.execute(update(PrintQueueItem).where(PrintQueueItem.id == ctx.item_id).values(status="pending"))
        await db.commit()
    _, _, _, gate2 = await _run(ctx)
    assert gate2.call_args.kwargs["ai_override"] is False
