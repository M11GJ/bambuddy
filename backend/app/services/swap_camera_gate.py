"""Pre-dispatch camera check for build-plate swapper printers.

Runs while the printer is idle in its post-swap pose, before anything heats.
Two checks: the plate's orange clip is visible (a plate is on the bed) and the
plate is empty compared with the calibrated reference. Anything we cannot
decide is a failure: printing on a bare bed costs far more than a stopped queue.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from backend.app.api.routes.camera import get_buffered_frame_with_time, is_stream_active
from backend.app.services.camera import capture_camera_frame_bytes
from backend.app.services.plate_detection import PlateDetector, is_plate_detection_available
from backend.app.services.printer_manager import printer_manager
from backend.app.services.swap_ai import AiVerdict, SwapAiSettings, ask_plate_empty, crop_plate
from backend.app.services.swap_config import SwapConfig
from backend.app.services.swap_gate_records import record_gate

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
    outcome: Literal["pass", "fail", "busy", "pending"]
    reason_key: str | None = None
    clip_percent: float | None = None
    diff_percent: float | None = None
    image: bytes | None = None
    ai_object: bool | None = None
    ai_reason: str | None = None
    ai_ms: int | None = None


@dataclass
class _AiWait:
    """An AI plate check in flight (or finished, not yet used) for one dispatch."""

    task: asyncio.Task
    image: bytes
    clip: float
    item_id: int | None
    first_pending_at: float
    asked_at: float
    last_polled_at: float


# Per-printer AI checks, kept in process memory across scheduler ticks: the
# HTTP call must never run inside _start_print's DB session.
_ai_waits: dict[int, _AiWait] = {}
# Shadow-mode checks are fire-and-forget; hold references so they are not GC'd.
_shadow_tasks: dict[int, asyncio.Task] = {}
# Fire-and-forget record writes (shadow results); hold references so they are not GC'd.
_record_tasks: set[asyncio.Task] = set()

# A wait nobody polled for this long (scheduler ticks every 30 s) belongs to a
# dispatch that went away; its verdict says nothing about the plate now.
_POLL_GAP_S = 120
# httpx's timeout is per phase, so a request can outlive timeout_s; past this
# margin the task counts as hung and its check as undecidable.
_HUNG_MARGIN_S = 30


def forget_ai_wait(printer_id: int) -> None:
    """Drop a printer's pending AI check, so the next dispatch takes a fresh photo."""
    w = _ai_waits.pop(printer_id, None)
    if w is not None:
        w.task.cancel()


async def _ask(ai: SwapAiSettings, image: bytes, roi) -> AiVerdict:
    try:
        jpeg = crop_plate(image, roi)
    except Exception as e:  # cv2.error, odd ROIs...: undecidable, never an exception out of the task
        return AiVerdict(None, "", 0, str(e) or type(e).__name__)
    return await ask_plate_empty(ai.url, ai.model, jpeg, ai.timeout_s)


def _start_shadow_check(printer, ai: SwapAiSettings, image: bytes, clip: float | None = None) -> None:
    running = _shadow_tasks.get(printer.id)
    if running and not running.done():
        return
    roi = _detector_roi(printer)
    task = asyncio.create_task(_ask(ai, image, roi))
    _shadow_tasks[printer.id] = task

    def _log(t: asyncio.Task) -> None:
        if not t.cancelled():
            v = t.result()
            logger.info(
                "Swap AI (shadow) printer %s: object=%s reason=%r ms=%s error=%s",
                printer.id,
                v.object_on_plate,
                v.reason,
                v.ms,
                v.error,
            )
            rec = asyncio.create_task(
                record_gate(
                    printer.id,
                    mode="shadow",
                    outcome="error" if v.object_on_plate is None else ("fail" if v.object_on_plate else "pass"),
                    reason_key="swap.gate.aiObject" if v.object_on_plate else None,
                    clip_percent=clip,
                    ai_object=v.object_on_plate,
                    ai_reason=v.error or v.reason,
                    ai_ms=v.ms,
                    image=image,
                    roi=roi,
                )
            )
            _record_tasks.add(rec)
            rec.add_done_callback(_record_tasks.discard)

    task.add_done_callback(_log)


def _poll_ai_wait(printer_id: int, item_id, ai: SwapAiSettings, now: float, roi) -> CameraGateResult | None:
    """Decide from a pending AI check; None means none usable, take a fresh photo."""
    w = _ai_waits.get(printer_id)
    if w is None:
        return None
    if w.item_id != item_id or now - w.last_polled_at > _POLL_GAP_S:
        forget_ai_wait(printer_id)
        return None
    w.last_polled_at = now
    if not w.task.done():
        if now - w.asked_at <= ai.timeout_s + _HUNG_MARGIN_S:
            return CameraGateResult("pending", "swap.gate.aiPending", w.clip, image=w.image)
        w.task.cancel()
        v = None
    else:
        v = None if w.task.cancelled() else w.task.result()
    verdict = v.object_on_plate if v else None
    detail = {"ai_object": verdict, "ai_reason": (v.error or v.reason) if v else None, "ai_ms": v.ms if v else None}
    if verdict is None:
        if now - w.first_pending_at <= ai.max_wait_s:
            w.task = asyncio.create_task(_ask(ai, w.image, roi))
            w.asked_at = now
            return CameraGateResult("pending", "swap.gate.aiPending", w.clip, image=w.image)
        del _ai_waits[printer_id]
        return CameraGateResult("fail", "swap.gate.aiUnavailable", w.clip, image=w.image, **detail)
    del _ai_waits[printer_id]
    if verdict:
        return CameraGateResult("fail", "swap.gate.aiObject", w.clip, image=w.image, **detail)
    return CameraGateResult("pass", None, w.clip, image=w.image, **detail)


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


async def run_camera_gate(
    printer, config: SwapConfig, *, ai: SwapAiSettings | None = None, **kwargs
) -> CameraGateResult:
    """Run the gate and record each terminal (pass/fail) decision."""
    result = await _run_camera_gate(printer, config, ai=ai, **kwargs)
    if result.outcome in ("pass", "fail"):
        mode = ai.mode if ai is not None and ai.url and ai.mode in ("shadow", "enforce") else "v1"
        await record_gate(
            printer.id,
            mode=mode,
            outcome=result.outcome,
            reason_key=result.reason_key,
            clip_percent=result.clip_percent,
            ai_object=result.ai_object,
            ai_reason=result.ai_reason,
            ai_ms=result.ai_ms,
            image=result.image,
            roi=_detector_roi(printer),
        )
    return result


async def _run_camera_gate(
    printer,
    config: SwapConfig,
    *,
    light_settle_seconds: float = 2.5,
    ai: SwapAiSettings | None = None,
    ai_override: bool = False,
    item_id: int | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CameraGateResult:
    """``ai`` None means v1 only. ``ai_override`` skips the AI verdict once (Task 3 wires it)."""
    if not is_plate_detection_available():
        return CameraGateResult("fail", "swap.gate.cameraUnavailable")
    if ai is not None and ai.mode == "enforce" and ai.url and not ai_override:
        polled = _poll_ai_wait(printer.id, item_id, ai, clock(), _detector_roi(printer))
        if polled is not None:
            return polled
    else:
        forget_ai_wait(printer.id)  # override or mode change: an old verdict must not linger
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
        if ai is not None and ai.mode == "enforce" and ai.url:
            if ai_override:
                return CameraGateResult("pass", None, clip, image=image)
            task = asyncio.create_task(_ask(ai, image, _detector_roi(printer)))
            await record_gate(  # once per wait: polls do not write
                printer.id,
                mode="enforce",
                outcome="pending",
                reason_key="swap.gate.aiPending",
                clip_percent=clip,
                image=image,
                roi=_detector_roi(printer),
            )
            now = clock()
            _ai_waits[printer.id] = _AiWait(task, image, clip, item_id, now, now, now)
            return CameraGateResult("pending", "swap.gate.aiPending", clip, image=image)
        if ai is not None and ai.mode == "shadow" and ai.url:
            _start_shadow_check(printer, ai, image, clip)
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
