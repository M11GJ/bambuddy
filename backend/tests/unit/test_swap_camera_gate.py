import asyncio
import time

import cv2
import numpy as np
import pytest

from backend.app.services import swap_camera_gate as gate
from backend.app.services.swap_config import DEFAULT_SWAP_CONFIG


def _jpeg(orange_in_clip: bool) -> bytes:
    img = np.full((480, 640, 3), (90, 90, 90), np.uint8)  # grey (BGR)
    if orange_in_clip:
        # inside clip ROI x 0.30-0.45, y 0.70-1.0 -> pixels 192-288, 336-480
        img[350:470, 200:280] = (20, 105, 255)  # orange in BGR
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


def test_clip_percent_detects_orange():
    assert gate.clip_percent(_jpeg(True), DEFAULT_SWAP_CONFIG.clip_roi) > 20
    assert gate.clip_percent(_jpeg(False), DEFAULT_SWAP_CONFIG.clip_roi) == 0


def test_clip_percent_raises_on_garbage():
    with pytest.raises(ValueError):
        gate.clip_percent(b"not an image", DEFAULT_SWAP_CONFIG.clip_roi)


class _Result:
    def __init__(self, is_empty, confidence=0.9, needs_calibration=False, diff=0.3):
        self.is_empty, self.confidence, self.needs_calibration, self.difference_percent = (
            is_empty,
            confidence,
            needs_calibration,
            diff,
        )


class _Printer:
    id, ip_address, access_code, model = 1, "192.0.2.1", "x", "A1"
    plate_detection_roi_x = plate_detection_roi_y = plate_detection_roi_w = plate_detection_roi_h = None


@pytest.fixture
def patched(monkeypatch):
    state = {"frame": _jpeg(True), "analysis": _Result(True), "stream": False, "buffered": None}

    async def capture(ip, code, model, timeout=10):
        return state["frame"]

    monkeypatch.setattr(gate, "capture_settled_frame_bytes", capture)
    monkeypatch.setattr(gate, "is_stream_active", lambda pid: state["stream"])
    monkeypatch.setattr(gate, "get_buffered_frame_with_time", lambda pid: state["buffered"])
    monkeypatch.setattr(gate, "is_plate_detection_available", lambda: True)
    monkeypatch.setattr(gate, "_set_light", lambda printer_id, on: None)
    monkeypatch.setattr(gate, "_light_is_on", lambda printer_id: True)

    async def no_record(*a, **k):
        pass

    monkeypatch.setattr(gate, "record_gate", no_record)

    class _Det:
        def __init__(self, roi=None, difference_threshold=1.0, normalize_lighting=False):
            state["threshold"] = difference_threshold
            state["normalize_lighting"] = normalize_lighting

        def analyze_frame(self, image, printer_id):
            if isinstance(state["analysis"], Exception):
                raise state["analysis"]
            return state["analysis"]

    monkeypatch.setattr(gate, "PlateDetector", _Det)
    return state


@pytest.mark.asyncio
async def test_pass(patched):
    r = await gate.run_camera_gate(_Printer(), DEFAULT_SWAP_CONFIG, light_settle_seconds=0)
    assert r.outcome == "pass"


@pytest.mark.asyncio
async def test_no_plate(patched):
    patched["frame"] = _jpeg(False)
    r = await gate.run_camera_gate(_Printer(), DEFAULT_SWAP_CONFIG, light_settle_seconds=0)
    assert (r.outcome, r.reason_key) == ("fail", "swap.gate.noPlate")


@pytest.mark.asyncio
async def test_object_on_plate(patched):
    patched["analysis"] = _Result(False)
    r = await gate.run_camera_gate(_Printer(), DEFAULT_SWAP_CONFIG, light_settle_seconds=0)
    assert r.reason_key == "swap.gate.objectOnPlate"


@pytest.mark.asyncio
async def test_detector_fail_open_is_not_a_pass(patched):
    patched["analysis"] = _Result(True, confidence=0.0)  # PlateDetector's error result
    r = await gate.run_camera_gate(_Printer(), DEFAULT_SWAP_CONFIG, light_settle_seconds=0)
    assert (r.outcome, r.reason_key) == ("fail", "swap.gate.cameraUnavailable")


@pytest.mark.asyncio
async def test_not_calibrated(patched):
    patched["analysis"] = _Result(True, needs_calibration=True)
    r = await gate.run_camera_gate(_Printer(), DEFAULT_SWAP_CONFIG, light_settle_seconds=0)
    assert r.reason_key == "swap.gate.notCalibrated"


@pytest.mark.asyncio
async def test_capture_failure(patched):
    patched["frame"] = None
    r = await gate.run_camera_gate(_Printer(), DEFAULT_SWAP_CONFIG, light_settle_seconds=0)
    assert (r.outcome, r.reason_key) == ("fail", "swap.gate.cameraUnavailable")


@pytest.fixture
def short_poll(monkeypatch):
    monkeypatch.setattr(gate, "_STREAM_FRESH_FRAME_WAIT_S", 0.3)
    monkeypatch.setattr(gate, "_STREAM_POLL_S", 0.05)


@pytest.mark.asyncio
async def test_streaming_without_any_frame_is_busy(patched, short_poll):
    patched["stream"] = True
    patched["buffered"] = None
    r = await gate.run_camera_gate(_Printer(), DEFAULT_SWAP_CONFIG, light_settle_seconds=0)
    assert (r.outcome, r.reason_key) == ("busy", "swap.gate.cameraBusy")


@pytest.mark.asyncio
async def test_streaming_frame_from_just_before_settle_is_busy(patched, short_poll):
    patched["stream"] = True
    # Stamped just before the gate starts, so before settled_at; no newer one arrives.
    patched["buffered"] = (_jpeg(True), time.time() - 0.01)
    r = await gate.run_camera_gate(_Printer(), DEFAULT_SWAP_CONFIG, light_settle_seconds=0)
    assert (r.outcome, r.reason_key) == ("busy", "swap.gate.cameraBusy")


@pytest.mark.asyncio
async def test_streaming_frame_arriving_during_poll_is_used(patched, short_poll, monkeypatch):
    patched["stream"] = True
    stale = (_jpeg(False), time.time() - 0.01)  # pre-light frame would fail noPlate
    calls = []

    def buffered(pid):
        calls.append(pid)
        # The stream delivers a new frame (stamped now, after settled_at) on the 3rd look.
        return stale if len(calls) < 3 else (_jpeg(True), time.time())

    monkeypatch.setattr(gate, "get_buffered_frame_with_time", buffered)
    r = await gate.run_camera_gate(_Printer(), DEFAULT_SWAP_CONFIG, light_settle_seconds=0)
    assert r.outcome == "pass"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_detector_exception_fails_closed(patched):
    patched["analysis"] = RuntimeError("boom")
    r = await gate.run_camera_gate(_Printer(), DEFAULT_SWAP_CONFIG, light_settle_seconds=0)
    assert (r.outcome, r.reason_key) == ("fail", "swap.gate.cameraUnavailable")


@pytest.mark.asyncio
async def test_opencv_unavailable_fails_closed(patched, monkeypatch):
    monkeypatch.setattr(gate, "is_plate_detection_available", lambda: False)
    r = await gate.run_camera_gate(_Printer(), DEFAULT_SWAP_CONFIG, light_settle_seconds=0)
    assert (r.outcome, r.reason_key) == ("fail", "swap.gate.cameraUnavailable")


@pytest.mark.asyncio
async def test_light_restored_when_it_was_off(patched, monkeypatch):
    calls = []
    monkeypatch.setattr(gate, "_light_is_on", lambda printer_id: False)
    monkeypatch.setattr(gate, "_set_light", lambda printer_id, on: calls.append(on))
    patched["frame"] = None  # even on failure the light goes back off
    await gate.run_camera_gate(_Printer(), DEFAULT_SWAP_CONFIG, light_settle_seconds=0)
    assert calls == [True, False]


@pytest.mark.asyncio
async def test_detector_uses_swap_object_threshold(patched):
    from dataclasses import replace

    await gate.run_camera_gate(
        _Printer(), replace(DEFAULT_SWAP_CONFIG, object_diff_percent=2.5), light_settle_seconds=0
    )
    assert patched["threshold"] == 2.5
    assert patched["normalize_lighting"] is True


# --- AI plate check (v2) ---

from backend.app.services.swap_ai import AiVerdict, SwapAiSettings  # noqa: E402


@pytest.fixture
async def ai(patched, monkeypatch):
    """AI answers come from patched["verdicts"] (popped in order); a fake clock."""
    gate._ai_waits.clear()
    gate._shadow_tasks.clear()
    patched["verdicts"] = []
    patched["asks"] = 0
    clock = {"t": 1000.0}

    async def ask(url, model, jpeg, timeout_s):
        patched["asks"] += 1
        return patched["verdicts"].pop(0)

    monkeypatch.setattr(gate, "ask_plate_empty", ask)
    patched["clock"] = clock
    yield lambda **kw: {
        "ai": SwapAiSettings(**{"mode": "enforce", "url": "http://ai", **kw}),
        "clock": lambda: clock["t"],
    }
    leftover = [w.task for w in gate._ai_waits.values()] + list(gate._shadow_tasks.values())
    for t in leftover:
        t.cancel()
    await asyncio.gather(*leftover, return_exceptions=True)
    gate._ai_waits.clear()
    gate._shadow_tasks.clear()


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def _run(**kw):
    return await gate.run_camera_gate(_Printer(), DEFAULT_SWAP_CONFIG, light_settle_seconds=0, **kw)


@pytest.mark.asyncio
async def test_enforce_pending_then_pass(ai, patched):
    patched["verdicts"] = [AiVerdict(False, "empty", 5, None)]
    patched["analysis"] = _Result(False)  # v1 would fail; enforce ignores it
    r = await _run(**ai())
    assert (r.outcome, r.reason_key) == ("pending", "swap.gate.aiPending")
    await _settle()
    patched["frame"] = None  # a pending verdict is used without a new photo
    r = await _run(**ai())
    assert r.outcome == "pass"
    assert not gate._ai_waits


@pytest.mark.asyncio
async def test_enforce_object_fails(ai, patched):
    patched["verdicts"] = [AiVerdict(True, "a part", 5, None)]
    await _run(**ai())
    await _settle()
    r = await _run(**ai())
    assert (r.outcome, r.reason_key) == ("fail", "swap.gate.aiObject")
    assert r.image == patched["frame"]


@pytest.mark.asyncio
async def test_enforce_in_flight_stays_pending(ai, patched, monkeypatch):
    never = asyncio.Event()

    async def ask(*a):
        await never.wait()

    monkeypatch.setattr(gate, "ask_plate_empty", ask)
    await _run(**ai())
    r = await _run(**ai())
    assert r.outcome == "pending"


@pytest.mark.asyncio
async def test_enforce_undecidable_retries_then_unavailable(ai, patched):
    patched["verdicts"] = [AiVerdict(None, "", 5, "timeout") for _ in range(4)]
    await _run(**ai())
    await _settle()
    for _ in range(3):  # t = 100, 200, 300: within max_wait_s (300), ask again
        patched["clock"]["t"] += 100
        assert (await _run(**ai())).outcome == "pending"
        await _settle()
    assert patched["asks"] == 4
    patched["clock"]["t"] += 100  # 400 s since first pending
    r = await _run(**ai())
    assert (r.outcome, r.reason_key) == ("fail", "swap.gate.aiUnavailable")
    assert not gate._ai_waits


@pytest.mark.asyncio
async def test_enforce_stale_wait_takes_new_photo(ai, patched):
    patched["verdicts"] = [AiVerdict(True, "old", 5, None), AiVerdict(False, "new", 5, None)]
    await _run(**ai())
    await _settle()
    patched["clock"]["t"] += 10_000
    assert (await _run(**ai())).outcome == "pending"
    await _settle()
    assert (await _run(**ai())).outcome == "pass"


@pytest.mark.asyncio
async def test_enforce_still_checks_clip(ai, patched):
    patched["frame"] = _jpeg(False)
    r = await _run(**ai())
    assert r.reason_key == "swap.gate.noPlate"
    assert patched["asks"] == 0


@pytest.mark.asyncio
async def test_enforce_override_skips_ai(ai, patched):
    patched["analysis"] = _Result(False)
    r = await _run(ai_override=True, **ai())
    assert r.outcome == "pass"
    await _settle()
    assert patched["asks"] == 0


@pytest.mark.asyncio
async def test_shadow_v1_decides(ai, patched):
    patched["verdicts"] = [AiVerdict(True, "a part", 5, None)]
    r = await _run(**ai(mode="shadow"))
    assert r.outcome == "pass"
    await _settle()
    assert patched["asks"] == 1  # ran in the background, not waited on


@pytest.mark.asyncio
async def test_shadow_v1_fail_still_fails(ai, patched):
    patched["verdicts"] = [AiVerdict(False, "empty", 5, None)]
    patched["analysis"] = _Result(False)
    r = await _run(**ai(mode="shadow"))
    assert r.reason_key == "swap.gate.objectOnPlate"


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", [{"mode": "off"}, {"mode": "enforce", "url": ""}, {"mode": "shadow", "url": ""}])
async def test_off_or_no_url_is_v1_only(ai, patched, kw):
    patched["analysis"] = _Result(False)
    r = await _run(**ai(**kw))
    assert r.reason_key == "swap.gate.objectOnPlate"
    await _settle()
    assert patched["asks"] == 0


def _hang(monkeypatch):
    async def ask(*a):
        await asyncio.Event().wait()

    monkeypatch.setattr(gate, "ask_plate_empty", ask)


@pytest.mark.asyncio
async def test_enforce_hanging_ai_ends_unavailable(ai, patched, monkeypatch):
    _hang(monkeypatch)
    r = await _run(**ai())
    outcomes = []
    for _ in range(20):  # 30 s scheduler ticks, 10 minutes
        patched["clock"]["t"] += 30
        r = await _run(**ai())
        outcomes.append(r.outcome)
        if r.outcome != "pending":
            break
    assert (r.outcome, r.reason_key) == ("fail", "swap.gate.aiUnavailable")
    assert len(outcomes) <= 12  # max_wait 300 s + hung margin, not forever


@pytest.mark.asyncio
async def test_enforce_wait_is_per_item(ai, patched):
    patched["verdicts"] = [AiVerdict(False, "empty", 5, None), AiVerdict(True, "part", 5, None)]
    await _run(item_id=1, **ai())
    await _settle()
    # A different queue item must not reuse item 1's verdict: new photo, new ask.
    assert (await _run(item_id=2, **ai())).outcome == "pending"
    await _settle()
    r = await _run(item_id=2, **ai())
    assert r.reason_key == "swap.gate.aiObject"
    assert patched["asks"] == 2


@pytest.mark.asyncio
async def test_hold_and_resume_forget_the_wait(ai, patched):
    from unittest.mock import AsyncMock

    from backend.app.services.printer_manager import printer_manager

    db = AsyncMock()
    for change in (
        lambda: printer_manager.set_swap_hold(db, _Printer.id, "swap.gate.noPlate", None),
        lambda: printer_manager.clear_swap_hold(db, _Printer.id),
    ):
        patched["verdicts"] = [AiVerdict(False, "empty", 5, None)]
        await _run(**ai())
        await _settle()
        await change()
        assert not gate._ai_waits


@pytest.mark.asyncio
async def test_mode_change_forgets_the_wait(ai, patched):
    patched["verdicts"] = [AiVerdict(False, "empty", 5, None)]
    await _run(**ai())
    await _run(**ai(mode="off"))
    assert not gate._ai_waits


@pytest.mark.asyncio
async def test_crop_error_is_undecidable(ai, patched, monkeypatch):
    def boom(image, roi):
        raise ZeroDivisionError

    monkeypatch.setattr(gate, "crop_plate", boom)
    await _run(**ai())
    await _settle()
    w = gate._ai_waits[_Printer.id]
    assert w.task.result().object_on_plate is None
    assert patched["asks"] == 0
