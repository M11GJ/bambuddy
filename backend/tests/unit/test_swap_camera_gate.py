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

    monkeypatch.setattr(gate, "capture_camera_frame_bytes", capture)
    monkeypatch.setattr(gate, "is_stream_active", lambda pid: state["stream"])
    monkeypatch.setattr(gate, "get_buffered_frame_with_time", lambda pid: state["buffered"])
    monkeypatch.setattr(gate, "is_plate_detection_available", lambda: True)
    monkeypatch.setattr(gate, "_set_light", lambda printer_id, on: None)
    monkeypatch.setattr(gate, "_light_is_on", lambda printer_id: True)

    class _Det:
        def __init__(self, roi=None):
            pass

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


@pytest.mark.asyncio
async def test_streaming_without_fresh_frame_is_busy(patched):
    patched["stream"] = True
    patched["buffered"] = None
    r = await gate.run_camera_gate(_Printer(), DEFAULT_SWAP_CONFIG, light_settle_seconds=0)
    assert (r.outcome, r.reason_key) == ("busy", "swap.gate.cameraBusy")


@pytest.mark.asyncio
async def test_streaming_stale_frame_is_busy(patched):
    patched["stream"] = True
    patched["buffered"] = (_jpeg(True), 0.0)  # taken long before the light came on
    r = await gate.run_camera_gate(_Printer(), DEFAULT_SWAP_CONFIG, light_settle_seconds=0)
    assert r.outcome == "busy"


@pytest.mark.asyncio
async def test_streaming_fresh_frame_is_used(patched):
    patched["stream"] = True
    patched["buffered"] = (_jpeg(True), time.time() + 1)  # landed after the light settled
    r = await gate.run_camera_gate(_Printer(), DEFAULT_SWAP_CONFIG, light_settle_seconds=0)
    assert r.outcome == "pass"


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
