import json
import zipfile
from pathlib import Path

import pytest

from backend.app.services.swap_config import DEFAULT_SWAP_CONFIG
from backend.app.services.swap_guard import check_swap_job

MARK = "; --- AUTO-CLEAR START ---"


def _make(tmp_path: Path, *, gcode: str, bbox_all=(118, 118, 138, 138), objects=None, plate=1, with_json=True) -> Path:
    p = tmp_path / "job.gcode.3mf"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(f"Metadata/plate_{plate}.gcode", gcode)
        if with_json:
            objs = objects if objects is not None else [{"name": "cube", "bbox": list(bbox_all)}]
            z.writestr(f"Metadata/plate_{plate}.json", json.dumps({"bbox_all": list(bbox_all), "bbox_objects": objs}))
    return p


GOOD = f"G28\nG1 X10\n{MARK}\nG1 Y265\n; EXECUTABLE_BLOCK_END\n; machine_end_gcode = ...{MARK}...\n"


def test_good_job_passes(tmp_path):
    assert check_swap_job(_make(tmp_path, gcode=GOOD), 1, DEFAULT_SWAP_CONFIG).ok


def test_missing_marker(tmp_path):
    r = check_swap_job(_make(tmp_path, gcode="G28\n; EXECUTABLE_BLOCK_END\n"), 1, DEFAULT_SWAP_CONFIG)
    assert (r.ok, r.reason_key) == (False, "swap.guard.noMarker")


def test_marker_only_in_config_comment_does_not_count(tmp_path):
    g = f"G28\n; EXECUTABLE_BLOCK_END\n{MARK}\n"
    assert check_swap_job(_make(tmp_path, gcode=g), 1, DEFAULT_SWAP_CONFIG).reason_key == "swap.guard.noMarker"


def test_two_markers(tmp_path):
    g = f"{MARK}\nG1\n  {MARK}\n; EXECUTABLE_BLOCK_END\n"
    assert check_swap_job(_make(tmp_path, gcode=g), 1, DEFAULT_SWAP_CONFIG).reason_key == "swap.guard.multipleMarkers"


@pytest.mark.parametrize(
    "bbox,ok",
    [
        ((10, 20.0, 50, 100), True),  # front edge exactly at the limit
        ((10, 19.9, 50, 100), False),  # into the front zone
        ((10, 100, 50, 246.0), True),  # back edge exactly at the limit
        ((10, 100, 50, 246.1), False),  # into the back zone
    ],
)
def test_exclusion_zones(tmp_path, bbox, ok):
    r = check_swap_job(_make(tmp_path, gcode=GOOD, bbox_all=bbox), 1, DEFAULT_SWAP_CONFIG)
    assert r.ok is ok
    if not ok:
        assert r.reason_key == "swap.guard.excludedArea"


def test_object_inside_but_bbox_all_outside_fails(tmp_path):
    objs = [{"name": "cube", "bbox": [100, 100, 120, 120]}]
    r = check_swap_job(_make(tmp_path, gcode=GOOD, bbox_all=(5, 10, 250, 200), objects=objs), 1, DEFAULT_SWAP_CONFIG)
    assert r.reason_key == "swap.guard.excludedArea"


def test_object_outside_named_in_detail(tmp_path):
    objs = [{"name": "tower", "bbox": [200, 240, 230, 250]}]
    r = check_swap_job(_make(tmp_path, gcode=GOOD, bbox_all=(100, 100, 150, 150), objects=objs), 1, DEFAULT_SWAP_CONFIG)
    assert r.reason_key == "swap.guard.excludedArea" and "tower" in (r.detail or "")


def test_missing_plate_json(tmp_path):
    r = check_swap_job(_make(tmp_path, gcode=GOOD, with_json=False), 1, DEFAULT_SWAP_CONFIG)
    assert r.reason_key == "swap.guard.unreadable"


def test_corrupt_zip(tmp_path):
    p = tmp_path / "bad.gcode.3mf"
    p.write_bytes(b"not a zip")
    assert check_swap_job(p, 1, DEFAULT_SWAP_CONFIG).reason_key == "swap.guard.unreadable"


def test_uses_requested_plate(tmp_path):
    p = _make(tmp_path, gcode=GOOD, plate=2)
    assert check_swap_job(p, 2, DEFAULT_SWAP_CONFIG).ok
