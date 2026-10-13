"""Per-printer configuration for build-plate swap (SwapMod-style) operation.

Stored as JSON in ``printers.swap_config``; ``None`` means "all defaults".
Invalid values raise instead of silently falling back: a typo in an
exclusion zone must stop the queue, not widen the printable area.
"""

import json
import math
import re
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class SwapConfig:
    marker: str
    exclude_front_mm: float
    exclude_back_mm: float
    bed_depth_mm: float
    clip_roi: tuple[float, float, float, float]
    clip_min_percent: float


DEFAULT_SWAP_CONFIG = SwapConfig(
    marker="; --- AUTO-CLEAR START ---",
    exclude_front_mm=20.0,
    exclude_back_mm=10.0,
    bed_depth_mm=256.0,
    clip_roi=(0.30, 0.70, 0.15, 0.30),
    clip_min_percent=4.0,
)

_FIELDS = {"marker", "exclude_front_mm", "exclude_back_mm", "bed_depth_mm", "clip_roi", "clip_min_percent"}


def _is_number(value) -> bool:
    # bool is an int subclass, and json.loads accepts NaN/Infinity; NaN in
    # particular would slip through every "< 0" range check below.
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def parse_swap_config(raw: str | None) -> SwapConfig:
    if not raw:
        return DEFAULT_SWAP_CONFIG
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"swap_config is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("swap_config must be a JSON object")
    unknown = set(data) - _FIELDS
    if unknown:
        raise ValueError(f"swap_config has unknown keys: {sorted(unknown)}")
    cfg = DEFAULT_SWAP_CONFIG
    if "marker" in data:
        if not isinstance(data["marker"], str) or not data["marker"].strip():
            raise ValueError("swap_config.marker must be a non-empty string")
        cfg = replace(cfg, marker=data["marker"])
    for key in ("exclude_front_mm", "exclude_back_mm", "bed_depth_mm", "clip_min_percent"):
        if key in data:
            if not _is_number(data[key]):
                raise ValueError(f"swap_config.{key} must be a finite number")
            cfg = replace(cfg, **{key: float(data[key])})
    if "clip_roi" in data:
        roi = data["clip_roi"]
        if not isinstance(roi, list) or len(roi) != 4 or not all(_is_number(v) for v in roi):
            raise ValueError("swap_config.clip_roi must be [x, y, w, h]")
        cfg = replace(cfg, clip_roi=tuple(float(v) for v in roi))
    x, y, w, h = cfg.clip_roi
    if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1 and x + w <= 1 and y + h <= 1):
        raise ValueError("swap_config.clip_roi must lie inside the image (fractions 0..1)")
    if cfg.exclude_front_mm < 0 or cfg.exclude_back_mm < 0 or cfg.bed_depth_mm <= 0:
        raise ValueError("swap_config exclusion zones and bed depth must be positive")
    if cfg.exclude_front_mm + cfg.exclude_back_mm >= cfg.bed_depth_mm:
        raise ValueError("swap_config exclusion zones leave no printable depth")
    if cfg.clip_min_percent <= 0:
        raise ValueError("swap_config.clip_min_percent must be > 0")
    return cfg


_KEY_PREFIX = re.compile(r"^\[[A-Za-z0-9_.]+\]\s*")


def strip_error_key(message: str | None) -> str | None:
    """Drop a leading ``[translation.key] `` so external channels get plain English."""
    if not message:
        return message
    return _KEY_PREFIX.sub("", message, count=1)
