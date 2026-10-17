"""Local vision-model check for the SwapMod plate gate (Ollama)."""

import base64
import json
import logging
import time
from dataclasses import dataclass

import httpx
from sqlalchemy import select

from backend.app.models.settings import Settings

# Optional like in swap_camera_gate: the scheduler imports this module, so it
# must import without OpenCV; crop_plate then raises ValueError (undecidable).
try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover - exercised only on installs without OpenCV
    cv2 = np = None

logger = logging.getLogger(__name__)

PROMPT = (
    "This is a camera view of a 3D printer build plate (the dark textured sheet). "
    "Ignore printed outline graphics that are part of the plate surface, the orange clip at the plate edge, "
    "reflections, and anything outside the plate. Is there any physical object (a printed part, filament, tool) "
    'lying on the build plate? Answer only JSON {"object_on_plate": true or false, "reason": "short"}.'
)
MAX_SIDE = 768


@dataclass
class AiVerdict:
    object_on_plate: bool | None
    reason: str
    ms: int
    error: str | None


@dataclass
class SwapAiSettings:
    mode: str = "shadow"
    url: str = ""
    model: str = "qwen2.5vl:7b"
    timeout_s: float = 60
    max_wait_s: float = 300


def crop_plate(image: bytes, roi: tuple[float, float, float, float] | None) -> bytes:
    """Crop to the ROI (fractions x, y, w, h; lower half when None), long side 768px, JPEG."""
    if cv2 is None:
        raise ValueError("OpenCV is not available")
    img = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("undecodable image")
    h, w = img.shape[:2]
    if roi is None:
        img = img[h // 2 :, :]
    else:
        x, y, rw, rh = roi
        img = img[int(h * y) : int(h * (y + rh)), int(w * x) : int(w * (x + rw))]
    h, w = img.shape[:2]
    scale = MAX_SIDE / max(h, w)
    if scale != 1:
        img = cv2.resize(img, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise ValueError("JPEG encode failed")
    return buf.tobytes()


async def ask_plate_empty(url: str, model: str, image_jpeg: bytes, timeout_s: float) -> AiVerdict:
    body = {
        "model": model,
        "prompt": PROMPT,
        "images": [base64.b64encode(image_jpeg).decode()],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
        "keep_alive": -1,
    }
    start = time.monotonic()

    def ms() -> int:
        return int((time.monotonic() - start) * 1000)

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(f"{url.rstrip('/')}/api/generate", json=body)
        resp.raise_for_status()
        answer = json.loads(resp.json()["response"])
        flag = answer["object_on_plate"]
        if not isinstance(flag, bool):
            return AiVerdict(None, "", ms(), "object_on_plate is not a boolean")
        return AiVerdict(flag, str(answer.get("reason", "")), ms(), None)
    except Exception as e:  # any failure means "cannot judge", never an exception into the gate
        logger.warning("Swap AI check failed: %s", e)
        return AiVerdict(None, "", ms(), str(e) or type(e).__name__)


async def load_swap_ai_settings(db) -> SwapAiSettings:
    keys = ["swap_ai_mode", "swap_ai_url", "swap_ai_model", "swap_ai_timeout_s", "swap_ai_max_wait_s"]
    rows = {r.key: r.value for r in (await db.execute(select(Settings).where(Settings.key.in_(keys)))).scalars()}
    d = SwapAiSettings()

    def num(key: str, default: float) -> float:
        try:
            return float(rows[key])
        except (KeyError, TypeError, ValueError):
            return default

    mode = rows.get("swap_ai_mode") or d.mode
    return SwapAiSettings(
        mode=mode if mode in ("off", "shadow", "enforce") else d.mode,
        url=(rows.get("swap_ai_url") or "").strip().rstrip("/"),
        model=(rows.get("swap_ai_model") or "").strip() or d.model,
        timeout_s=num("swap_ai_timeout_s", d.timeout_s),
        max_wait_s=num("swap_ai_max_wait_s", d.max_wait_s),
    )
