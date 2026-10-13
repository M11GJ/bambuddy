"""Pre-dispatch check that a print file is safe for a build-plate swapper.

A swapper printer must only receive files sliced with its swap preset: the
swap block has to run exactly once (in the executed part of the file) and no
geometry may sit in the zones the swapper's clips and hooks occupy.
"""

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

from backend.app.services.swap_config import SwapConfig
from backend.app.utils.threemf_tools import select_plate_gcode_name

# Everything after this line is slicer config echoed as comments, not executed.
_EXEC_END = "; EXECUTABLE_BLOCK_END"


@dataclass(frozen=True)
class SwapGuardResult:
    ok: bool
    reason_key: str | None = None
    detail: str | None = None


def _fail(key: str, detail: str | None = None) -> SwapGuardResult:
    return SwapGuardResult(ok=False, reason_key=key, detail=detail)


def check_swap_job(threemf_path: Path, plate_id: int, config: SwapConfig) -> SwapGuardResult:
    try:
        with zipfile.ZipFile(threemf_path) as z:
            names = z.namelist()
            gcode_name = select_plate_gcode_name(names, plate_id)
            if not gcode_name:
                return _fail("swap.guard.unreadable", f"plate {plate_id} G-code not found")
            gcode = z.read(gcode_name).decode("utf-8", errors="ignore")
            json_name = f"Metadata/plate_{plate_id}.json"
            if json_name not in names:
                return _fail("swap.guard.unreadable", f"{json_name} not found")
            plate_meta = json.loads(z.read(json_name))
    except (zipfile.BadZipFile, OSError, KeyError, json.JSONDecodeError, UnicodeDecodeError) as e:
        return _fail("swap.guard.unreadable", str(e))

    # Count only the executed part: the marker also appears inside the echoed
    # machine_end_gcode config comment, which must not count as a second block.
    end = gcode.find(_EXEC_END)
    executed = gcode if end == -1 else gcode[:end]
    marker = config.marker.strip()
    count = sum(1 for line in executed.splitlines() if line.strip().startswith(marker))
    if count == 0:
        return _fail("swap.guard.noMarker")
    if count > 1:
        return _fail("swap.guard.multipleMarkers", f"{count} swap blocks")

    front = config.exclude_front_mm
    back_limit = config.bed_depth_mm - config.exclude_back_mm
    # bbox_all is checked as well as each object: it is the plate's real extent,
    # so a stale or incomplete per-object list cannot hide geometry in a zone.
    boxes: list[tuple[str, list]] = []
    if "bbox_all" in plate_meta:
        boxes.append(("(all)", plate_meta["bbox_all"]))
    for obj in plate_meta.get("bbox_objects", []) or []:
        boxes.append((str(obj.get("name", "?")), obj.get("bbox")))
    if not boxes:
        return _fail("swap.guard.unreadable", "plate has no bounding boxes")
    for name, bbox in boxes:
        if not isinstance(bbox, list) or len(bbox) != 4:
            return _fail("swap.guard.unreadable", f"bad bbox for {name}")
        y0, y1 = float(bbox[1]), float(bbox[3])
        if y0 < front or y1 > back_limit:
            return _fail(
                "swap.guard.excludedArea", f"{name}: Y {y0:.1f}-{y1:.1f} mm (allowed {front:g}-{back_limit:g})"
            )
    return SwapGuardResult(ok=True)
