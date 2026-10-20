"""Pre-dispatch check that a print file is safe for a build-plate swapper.

A swapper printer must only receive files sliced with its swap preset: the
swap block has to run exactly once (in the executed part of the file) and no
geometry may sit in the zones the swapper's clips and hooks occupy.
"""

import asyncio
import json
import math
import zipfile
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.printer import Printer
from backend.app.services.swap_config import SwapConfig, parse_swap_config
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
    except Exception as e:  # fail closed: encrypted members, zlib.error, EOFError etc. must not escape
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
    # Sliced before the swapper profile dropped the XY home: `G28 X` homes XY
    # on Bambu (the rear Y home pulls a spare plate out of the holder), a bare
    # `G28` runs when bed levelling is off, and `M18`
    # releases the motors so the next print would have to home again.
    # The swap block itself may home XY: the bed is empty there and the rear
    # home doubles as picking up the next plate.
    for line in executed.splitlines():
        if line.strip().startswith(marker):
            break
        cmd = line.split(";", 1)[0].strip().upper()
        # Only a Z-only home is safe; G28, G28 X and G28 Y all home Y at the back.
        homes_xy = (cmd == "G28" or cmd.startswith("G28 ")) and not cmd.startswith("G28 Z")
        if homes_xy or cmd == "M18" or cmd.startswith("M18 "):
            return _fail("swap.guard.staleProfile", cmd)

    if not isinstance(plate_meta, dict):
        return _fail("swap.guard.unreadable", "plate metadata is not an object")
    # Without bbox_all we cannot vouch for the plate's real extent.
    if "bbox_all" not in plate_meta:
        return _fail("swap.guard.unreadable", "plate metadata has no bbox_all")

    front = config.exclude_front_mm
    back_limit = config.bed_depth_mm - config.exclude_back_mm
    # bbox_all is checked as well as each object: it is the plate's real extent,
    # so a stale or incomplete per-object list cannot hide geometry in a zone.
    boxes: list[tuple[str, object]] = [("(all)", plate_meta["bbox_all"])]
    objects = plate_meta.get("bbox_objects") or []
    if not isinstance(objects, list):
        return _fail("swap.guard.unreadable", "bbox_objects is not a list")
    for obj in objects:
        if not isinstance(obj, dict):
            return _fail("swap.guard.unreadable", "bbox_objects entry is not an object")
        boxes.append((str(obj.get("name", "?")), obj.get("bbox")))
    for name, bbox in boxes:
        if not isinstance(bbox, list) or len(bbox) != 4:
            return _fail("swap.guard.unreadable", f"bad bbox for {name}")
        try:
            vals = [float(v) for v in bbox]
        except (TypeError, ValueError):
            return _fail("swap.guard.unreadable", f"non-numeric bbox for {name}")
        # NaN compares false against every limit and would otherwise pass.
        if not all(math.isfinite(v) for v in vals):
            return _fail("swap.guard.unreadable", f"non-finite bbox for {name}")
        y0, y1 = vals[1], vals[3]
        if y0 < front or y1 > back_limit:
            return _fail(
                "swap.guard.excludedArea", f"{name}: Y {y0:.1f}-{y1:.1f} mm (allowed {front:g}-{back_limit:g})"
            )
    return SwapGuardResult(ok=True)


async def enqueue_swap_guard(
    db: AsyncSession, printer_id: int | None, file_path: Path | None, plate_id: int | None
) -> None:
    """Reject at enqueue time a file a swap printer's dispatch guard would refuse.

    No-op for no printer or a non-swap printer. For a swap printer a missing or
    unreadable file (including unsliced ones) fails closed as swap.guard.unreadable.
    """
    if printer_id is None:
        return
    printer = (await db.execute(select(Printer).where(Printer.id == printer_id))).scalar_one_or_none()
    if printer is None or not printer.swap_enabled:
        return
    try:
        cfg = parse_swap_config(printer.swap_config)
    except ValueError as e:
        raise HTTPException(422, detail=f"[swap.hold.configInvalid] Swap printer configuration is invalid: {e}") from e
    if file_path is None:
        raise HTTPException(422, detail="[swap.guard.unreadable] Print file rejected for the swap printer: no file")
    from backend.app.services.print_scheduler import _effective_plate_id  # lazy: scheduler imports this module

    def _check() -> SwapGuardResult:
        return check_swap_job(file_path, _effective_plate_id(plate_id, file_path), cfg)

    guard = await asyncio.to_thread(_check)
    if not guard.ok:
        raise HTTPException(
            422,
            detail=f"[{guard.reason_key}] Print file rejected for the swap printer"
            + (f": {guard.detail}" if guard.detail else ""),
        )
