"""Plate-check records: one row + cropped image per decision, 50 kept, printers:update only."""

import cv2
import numpy as np
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.config import settings
from backend.app.models.swap_gate_record import SwapGateRecord
from backend.app.services.swap_gate_records import KEEP_PER_PRINTER, record_gate
from backend.tests.integration.test_swap_api import _token, auth  # noqa: F401


@pytest.fixture(autouse=True)
def _own_sessions(db_session, monkeypatch):
    # record_gate opens its own session; point it at the test engine.
    monkeypatch.setattr(
        "backend.app.core.database.async_session",
        async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False),
    )


@pytest.fixture(autouse=True)
def gate_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "swap_gate_dir", tmp_path / "swap_gate")
    return tmp_path / "swap_gate"


def _frame() -> bytes:
    ok, buf = cv2.imencode(".jpg", np.full((1000, 2000, 3), 90, np.uint8))
    assert ok
    return buf.tobytes()


async def _rows(db_session, pid):
    db_session.expire_all()
    return (
        (
            await db_session.execute(
                select(SwapGateRecord).where(SwapGateRecord.printer_id == pid).order_by(SwapGateRecord.id)
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_record_keeps_row_and_cropped_image(db_session, printer_factory, gate_dir):
    p = await printer_factory()
    await record_gate(
        p.id, mode="enforce", outcome="pass", clip_percent=12.5, ai_object=False, ai_ms=7,
        image=_frame(), roi=(0.0, 0.0, 0.5, 0.5),
    )  # fmt: skip
    (row,) = await _rows(db_session, p.id)
    assert (row.mode, row.outcome, row.clip_percent, row.ai_object) == ("enforce", "pass", 12.5, False)
    img = cv2.imread(str(gate_dir / row.image_path))
    assert max(img.shape[:2]) == 768  # 1000x500 crop -> long side 768, not the 2000px frame


@pytest.mark.asyncio
@pytest.mark.integration
async def test_51st_record_prunes_oldest_row_and_file(db_session, printer_factory, gate_dir):
    p = await printer_factory()
    for _ in range(KEEP_PER_PRINTER + 1):
        await record_gate(p.id, mode="v1", outcome="fail", image=_frame())
    rows = await _rows(db_session, p.id)
    assert len(rows) == KEEP_PER_PRINTER
    assert rows[0].id > 1
    assert len(list(gate_dir.glob("*.jpg"))) == KEEP_PER_PRINTER


@pytest.mark.asyncio
@pytest.mark.integration
async def test_records_api_permissions_and_delete(
    async_client: AsyncClient, db_session, printer_factory, auth, gate_dir
):
    pid = (await printer_factory()).id
    await record_gate(pid, mode="v1", outcome="pass", image=_frame())
    (row,) = await _rows(db_session, pid)
    viewer = await _token(async_client, db_session, "viewer", ["camera:view", "printers:read"])
    updater = await _token(async_client, db_session, "updater", ["printers:update", "printers:read"])
    base = f"/api/v1/printers/{pid}/swap/records"
    assert (await async_client.get(base, headers=viewer)).status_code == 403
    assert (await async_client.get(f"{base}/{row.id}/image", headers=viewer)).status_code == 403
    assert (await async_client.delete(base, headers=viewer)).status_code == 403

    r = await async_client.get(base, headers=updater)
    assert r.status_code == 200 and r.json()[0]["id"] == row.id and r.json()[0]["has_image"]
    img = await async_client.get(f"{base}/{row.id}/image", headers=updater)
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"

    assert (await async_client.delete(base, headers=updater)).json() == {"deleted": 1}
    assert await _rows(db_session, pid) == []
    assert list(gate_dir.glob("*.jpg")) == []
