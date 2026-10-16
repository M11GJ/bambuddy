"""Pre-dispatch camera check for build-plate swapper printers.

Runs while the printer is idle in its post-swap pose, before anything heats.
Two checks: the plate's orange clip is visible (a plate is on the bed) and the
plate is empty compared with the calibrated reference. Anything we cannot
decide is a failure: printing on a bare bed costs far more than a stopped queue.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Literal

from backend.app.api.routes.camera import get_buffered_frame_with_time, is_stream_active
from backend.app.services.camera import capture_camera_frame_bytes
from backend.app.services.plate_detection import PlateDetector, is_plate_detection_available
from backend.app.services.printer_manager import printer_manager
from backend.app.services.swap_config import SwapConfig

# OpenCV is optional in this codebase (see plate_detection); without it the
# gate must still import and then fail closed, not crash the dispatcher.
try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover - exercised only on installs without OpenCV
    cv2 = np = None

logger = logging.getLogger(__name__)

# HSV range of the orange swap clips (OpenCV hue 0-179), measured on the DCC A1.
_ORANGE_LOW = (5, 140, 90)
_ORANGE_HIGH = (20, 255, 255)

# How long to wait for a live-stream frame taken after the light settled, and
# how often to look. A few fps stream delivers one well within this window.
_STREAM_FRESH_FRAME_WAIT_S = 2.0
_STREAM_POLL_S = 0.2


@dataclass(frozen=True)
class CameraGateResult:
    outcome: Literal["pass", "fail", "busy"]
    reason_key: str | None = None
    clip_percent: float | None = None
    diff_percent: float | None = None
    image: bytes | None = None


def clip_percent(image_bytes: bytes, roi: tuple[float, float, float, float]) -> float:
    """Percentage of orange-clip pixels inside ``roi`` (fractions of the image)."""
    if cv2 is None:
        raise ValueError("OpenCV is not available")
    img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("image could not be decoded")
    h, w = img.shape[:2]
    x, y, rw, rh = roi
    crop = img[int(y * h) : int((y + rh) * h), int(x * w) : int((x + rw) * w)]
    if crop.size == 0:
        raise ValueError("clip ROI is empty")
    mask = cv2.inRange(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV), _ORANGE_LOW, _ORANGE_HIGH)
    return float(mask.mean() / 255 * 100)


def _light_is_on(printer_id: int) -> bool:
    state = printer_manager.get_status(printer_id)
    return bool(state and state.chamber_light)


def _set_light(printer_id: int, on: bool) -> None:
    # Light trouble must not turn into an exception out of the gate: a dark
    # frame fails the clip check anyway, so the gate still fails closed.
    client = printer_manager.get_client(printer_id)
    if client:
        try:
            client.set_chamber_light(on)
        except Exception as e:
            logger.warning("Swap gate: could not set chamber light %s for printer %s: %s", on, printer_id, e)


def _detector_roi(printer) -> tuple[float, float, float, float] | None:
    vals = (
        printer.plate_detection_roi_x,
        printer.plate_detection_roi_y,
        printer.plate_detection_roi_w,
        printer.plate_detection_roi_h,
    )
    return None if any(v is None for v in vals) else tuple(vals)


async def run_camera_gate(printer, config: SwapConfig, *, light_settle_seconds: float = 2.5) -> CameraGateResult:
    if not is_plate_detection_available():
        return CameraGateResult("fail", "swap.gate.cameraUnavailable")
    light_was_on = _light_is_on(printer.id)
    if not light_was_on:
        _set_light(printer.id, True)
    # Wall clock, because the stream buffer stamps frames with time.time().
    settled_at = time.time() + light_settle_seconds
    try:
        await asyncio.sleep(light_settle_seconds)
        if is_stream_active(printer.id):
            # A viewer holds the printer's only camera connection; opening a
            # second one would kick it off (#1348). Use the stream's frame, but
            # only one taken after the light settled - otherwise retry later.
            # The newest buffered frame predates settled_at right after the
            # sleep, so wait a few frame intervals for one taken after it.
            deadline = time.monotonic() + _STREAM_FRESH_FRAME_WAIT_S
            while True:
                buffered = get_buffered_frame_with_time(printer.id)
                if buffered is not None and buffered[1] >= settled_at:
                    image = buffered[0]
                    break
                if time.monotonic() >= deadline:
                    return CameraGateResult("busy", "swap.gate.cameraBusy")
                await asyncio.sleep(_STREAM_POLL_S)
        else:
            # Coalesced capture: joining one started before the light settled can return a pre-light (dark) frame, which fails closed via the clip check.
            image = await capture_camera_frame_bytes(printer.ip_address, printer.access_code, printer.model, timeout=10)
        if not image:
            return CameraGateResult("fail", "swap.gate.cameraUnavailable")
        try:
            clip = clip_percent(image, config.clip_roi)
        except ValueError:
            return CameraGateResult("fail", "swap.gate.cameraUnavailable", image=image)
        if clip < config.clip_min_percent:
            return CameraGateResult("fail", "swap.gate.noPlate", clip_percent=clip, image=image)
        try:
            result = PlateDetector(
                roi=_detector_roi(printer), difference_threshold=config.object_diff_percent, normalize_lighting=True
            ).analyze_frame(image, printer.id)
        except Exception:
            logger.exception("Swap gate: plate detection failed for printer %s", printer.id)
            return CameraGateResult("fail", "swap.gate.cameraUnavailable", clip_percent=clip, image=image)
        if result.needs_calibration:
            return CameraGateResult("fail", "swap.gate.notCalibrated", clip_percent=clip, image=image)
        if not result.is_empty:
            return CameraGateResult("fail", "swap.gate.objectOnPlate", clip, result.difference_percent, image)
        if not result.confidence or result.confidence <= 0:
            # PlateDetector reports decode/analysis errors as an "empty" plate
            # with zero confidence (it fails open for the normal print flow);
            # never treat that as a pass here.
            return CameraGateResult("fail", "swap.gate.cameraUnavailable", clip, result.difference_percent, image)
        return CameraGateResult("pass", None, clip, result.difference_percent, image)
    finally:
        if not light_was_on:
            _set_light(printer.id, False)
