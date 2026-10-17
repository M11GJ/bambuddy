import json

import pytest

from backend.app.services.swap_config import DEFAULT_SWAP_CONFIG, parse_swap_config, strip_error_key


def test_none_and_empty_give_defaults():
    assert parse_swap_config(None) == DEFAULT_SWAP_CONFIG
    assert parse_swap_config("") == DEFAULT_SWAP_CONFIG
    assert DEFAULT_SWAP_CONFIG.marker == "; --- AUTO-CLEAR START ---"
    assert DEFAULT_SWAP_CONFIG.exclude_front_mm == 20
    assert DEFAULT_SWAP_CONFIG.exclude_back_mm == 10
    assert DEFAULT_SWAP_CONFIG.bed_depth_mm == 256
    assert DEFAULT_SWAP_CONFIG.clip_roi == (0.30, 0.70, 0.15, 0.30)
    assert DEFAULT_SWAP_CONFIG.clip_min_percent == 4.0


def test_partial_override_keeps_other_defaults():
    cfg = parse_swap_config(json.dumps({"exclude_front_mm": 25, "clip_min_percent": 6}))
    assert cfg.exclude_front_mm == 25
    assert cfg.clip_min_percent == 6
    assert cfg.exclude_back_mm == 10


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps([1, 2]),
        json.dumps({"marker": ""}),
        json.dumps({"exclude_front_mm": -1}),
        json.dumps({"bed_depth_mm": 10, "exclude_front_mm": 6, "exclude_back_mm": 5}),
        json.dumps({"clip_roi": [0.5, 0.5, 0.6, 0.1]}),
        json.dumps({"clip_roi": [0.1, 0.1, 0.1]}),
        json.dumps({"clip_min_percent": 0}),
        json.dumps({"unknown_key": 1}),
        '{"exclude_front_mm": NaN}',
        json.dumps({"bed_depth_mm": True}),
    ],
)
def test_invalid_config_raises(raw):
    with pytest.raises(ValueError):
        parse_swap_config(raw)


def test_strip_error_key():
    assert strip_error_key("[swap.guard.noMarker] No swap block.") == "No swap block."
    assert strip_error_key("Plain message") == "Plain message"
    assert strip_error_key(None) is None


def test_object_diff_percent_default_and_override():
    assert DEFAULT_SWAP_CONFIG.object_diff_percent == 3.0
    assert parse_swap_config('{"object_diff_percent": 5}').object_diff_percent == 5.0


@pytest.mark.parametrize("v", [0, -1, 101, None, "3"])
def test_object_diff_percent_rejects_bad_values(v):
    import json

    with pytest.raises(ValueError):
        parse_swap_config(json.dumps({"object_diff_percent": v}))


def test_plate_detector_keeps_twenty_references():
    from backend.app.services.plate_detection import PlateDetector

    assert PlateDetector.MAX_REFERENCES == 20
