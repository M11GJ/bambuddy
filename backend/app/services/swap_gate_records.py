"""Store plate-check decisions with the cropped image the AI saw (newest 500 per printer)."""

import asyncio
import logging
import time
import uuid

from sqlalchemy import delete, select

from backend.app.core import database
from backend.app.core.config import settings
from backend.app.models.swap_gate_record import SwapGateRecord
from backend.app.services.swap_ai import crop_plate

logger = logging.getLogger(__name__)

KEEP_PER_PRINTER = 500


def _write_crop(image: bytes, roi) -> str | None:
    jpeg = crop_plate(image, roi)
    settings.swap_gate_dir.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.jpg"
    (settings.swap_gate_dir / name).write_bytes(jpeg)  # SEC-PATH-OK: our own uuid name
    return name


def _unlink(names) -> None:
    for n in names:
        if n:
            (settings.swap_gate_dir / n).unlink(missing_ok=True)  # SEC-PATH-OK: names are our own uuid files


_ORPHAN_MIN_AGE_S = 60  # a file this young may belong to a row still being inserted


def _sweep_orphans(keep: set[str]) -> None:
    """Remove image files no row references (e.g. left by a deleted printer)."""
    now = time.time()
    for f in settings.swap_gate_dir.glob("*.jpg"):
        if f.name not in keep and now - f.stat().st_mtime > _ORPHAN_MIN_AGE_S:
            f.unlink(missing_ok=True)


async def record_gate(
    printer_id: int,
    *,
    mode: str,
    outcome: str,
    reason_key: str | None = None,
    clip_percent: float | None = None,
    ai_object: bool | None = None,
    ai_reason: str | None = None,
    ai_ms: int | None = None,
    image: bytes | None = None,
    roi=None,
) -> None:
    """Insert one row (own DB session) and prune; never raises, the gate must not depend on it."""
    name = None
    try:
        if image:
            try:
                name = await asyncio.to_thread(_write_crop, image, roi)
            except Exception as e:  # undecodable frame: keep the row without an image
                logger.warning("Swap gate record: no crop for printer %s: %s", printer_id, e)
        async with database.async_session() as db:
            db.add(
                SwapGateRecord(
                    printer_id=printer_id,
                    mode=mode,
                    clip_percent=clip_percent,
                    ai_object=ai_object,
                    ai_reason=ai_reason,
                    ai_ms=ai_ms,
                    outcome=outcome,
                    reason_key=reason_key,
                    image_path=name,
                )
            )
            await db.flush()
            old = (
                await db.execute(
                    select(SwapGateRecord.id, SwapGateRecord.image_path)
                    .where(SwapGateRecord.printer_id == printer_id)
                    .order_by(SwapGateRecord.id.desc())
                    .offset(KEEP_PER_PRINTER)
                )
            ).all()
            if old:
                await db.execute(delete(SwapGateRecord).where(SwapGateRecord.id.in_([r.id for r in old])))
            await db.commit()
            keep = {r for (r,) in (await db.execute(select(SwapGateRecord.image_path))).all() if r}
        name = None  # committed: the row owns the image now, no later failure may delete it
    except Exception:
        logger.exception("Swap gate record failed for printer %s", printer_id)
        await asyncio.to_thread(_unlink, [name])
        return
    try:
        await asyncio.to_thread(_unlink, [r.image_path for r in old])
        await asyncio.to_thread(_sweep_orphans, keep)
    except Exception:
        logger.warning("Swap gate record cleanup failed for printer %s", printer_id, exc_info=True)


async def delete_records(db, printer_id: int) -> int:
    """Delete all of a printer's rows and their image files."""
    rows = (
        await db.execute(
            select(SwapGateRecord.id, SwapGateRecord.image_path).where(SwapGateRecord.printer_id == printer_id)
        )
    ).all()
    await db.execute(delete(SwapGateRecord).where(SwapGateRecord.printer_id == printer_id))
    await db.commit()
    await asyncio.to_thread(_unlink, [r.image_path for r in rows])
    return len(rows)
