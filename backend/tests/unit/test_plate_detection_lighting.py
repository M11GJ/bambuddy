"""Swap gate compares plates under changing room light: the z-score option must
ignore a lighting gradient but still see an object."""

import importlib

import cv2
import numpy as np

from backend.app.services import plate_detection


def _plate(gain: float, gradient: float, obj: bool = False) -> np.ndarray:
    h, w = 360, 512
    yy, xx = np.mgrid[0:h, 0:w]
    base = 110 + 25 * np.sin(xx / 60.0) * np.cos(yy / 45.0)  # plate pattern and fixtures
    base = base.astype(np.float32) * np.linspace(1.0, 1.0 - gradient, h)[:, None] * gain
    if obj:
        cv2.rectangle(base, (200, 140), (300, 240), 200 * gain, -1)
    return cv2.cvtColor(np.clip(base, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)


def _diff(det, a: np.ndarray, b: np.ndarray) -> float:
    pa, pb = det._preprocess_for_comparison(a), det._preprocess_for_comparison(b)
    return float(np.mean(cv2.absdiff(pa, pb)) / 255 * 100)


def test_zscore_ignores_lighting_gradient_but_sees_object():
    # test_plate_detection.py reloads this module against mocked cv2/numpy and leaves it that way.
    PlateDetector = importlib.reload(plate_detection).PlateDetector
    day, night, night_obj = _plate(1.0, 0.0), _plate(0.45, 0.05), _plate(0.45, 0.05, obj=True)
    plain = PlateDetector()
    lit = PlateDetector(normalize_lighting=True)
    # Swap gate threshold is 3%: the plain min-max version crosses it for an empty
    # plate at night (as on the real A1 camera: 8.3% plain vs 2.1% z-score).
    assert _diff(plain, day, night) > 3.0
    assert _diff(lit, day, night) < 3.0
    assert _diff(lit, day, night_obj) > 3.0
