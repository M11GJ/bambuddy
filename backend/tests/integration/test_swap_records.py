"""Plate-check records: one row + cropped image per decision, 500 kept, printers:update only."""

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
async def test_record_over_cap_prunes_oldest_row_and_file(db_session, printer_factory, gate_dir, monkeypatch):
    assert KEEP_PER_PRINTER == 500
    monkeypatch.setattr("backend.app.services.swap_gate_records.KEEP_PER_PRINTER", 3)  # keep the test fast
    p = await printer_factory()
    for _ in range(4):
        await record_gate(p.id, mode="v1", outcome="fail", image=_frame())
    rows = await _rows(db_session, p.id)
    assert len(rows) == 3
    assert rows[0].id > 1
    assert len(list(gate_dir.glob("*.jpg"))) == 3


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sweep_failure_keeps_committed_image(db_session, printer_factory, gate_dir, monkeypatch):
    def boom(keep):
        raise OSError("sweep broke")

    monkeypatch.setattr("backend.app.services.swap_gate_records._sweep_orphans", boom)
    p = await printer_factory()
    await record_gate(p.id, mode="v1", outcome="fail", image=_frame())
    (row,) = await _rows(db_session, p.id)
    assert row.image_path and (gate_dir / row.image_path).exists()


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
    assert r.status_code == 200 and r.json()["items"][0]["id"] == row.id and r.json()["items"][0]["has_image"]
    img = await async_client.get(f"{base}/{row.id}/image", headers=updater)
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"

    assert (await async_client.delete(base, headers=updater)).json() == {"deleted": 1}
    assert await _rows(db_session, pid) == []
    assert list(gate_dir.glob("*.jpg")) == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_records_api_pages_newest_first_with_total(async_client, db_session, printer_factory, auth):
    pid = (await printer_factory()).id
    db_session.add_all([SwapGateRecord(printer_id=pid, mode="v1", outcome="pass") for _ in range(60)])
    await db_session.commit()
    ids = sorted(r.id for r in await _rows(db_session, pid))
    base = f"/api/v1/printers/{pid}/swap/records"

    body = (await async_client.get(base, headers=auth)).json()  # default limit 50
    assert body["total"] == 60 and [i["id"] for i in body["items"]] == ids[::-1][:50]
    body = (await async_client.get(base, params={"limit": 5, "offset": 10}, headers=auth)).json()
    assert body["total"] == 60 and [i["id"] for i in body["items"]] == ids[::-1][10:15]
    assert (await async_client.get(base, params={"offset": 100}, headers=auth)).json() == {"total": 60, "items": []}
    for bad in ({"limit": 0}, {"limit": 501}, {"offset": -1}):
        assert (await async_client.get(base, params=bad, headers=auth)).status_code == 422


# --- the real record_gate behind run_camera_gate (no stub) ---

import asyncio  # noqa: E402
import os  # noqa: E402
import time  # noqa: E402

from backend.app.services import swap_camera_gate as gate  # noqa: E402
from backend.app.services.swap_ai import AiVerdict, SwapAiSettings  # noqa: E402
from backend.app.services.swap_config import DEFAULT_SWAP_CONFIG  # noqa: E402
from backend.tests.unit.test_swap_camera_gate import _jpeg, _Printer, _Result  # noqa: E402


@pytest.fixture
def real_gate(monkeypatch, printer_factory):
    state = {"empty": True}

    async def capture(ip, code, model, timeout=10):
        return _jpeg(True)

    class _Det:
        def __init__(self, **kw):
            pass

        def analyze_frame(self, image, printer_id):
            return _Result(state["empty"])

    monkeypatch.setattr(gate, "capture_settled_frame_bytes", capture)
    monkeypatch.setattr(gate, "is_stream_active", lambda pid: False)
    monkeypatch.setattr(gate, "is_plate_detection_available", lambda: True)
    monkeypatch.setattr(gate, "_set_light", lambda *a: None)
    monkeypatch.setattr(gate, "_light_is_on", lambda pid: True)
    monkeypatch.setattr(gate, "PlateDetector", _Det)
    gate._ai_waits.clear()
    return state


async def _printer(printer_factory):
    p = _Printer()
    p.id = (await printer_factory()).id
    return p


async def _run(p, **kw):
    r = await gate.run_camera_gate(p, DEFAULT_SWAP_CONFIG, light_settle_seconds=0, **kw)
    await gate.drain_records()
    return r


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("ai", [None, SwapAiSettings(mode="off", url="http://x")])
async def test_gate_v1_pass_and_fail_each_write_a_row(real_gate, printer_factory, db_session, gate_dir, ai):
    p = await _printer(printer_factory)
    assert (await _run(p, ai=ai)).outcome == "pass"
    real_gate["empty"] = False
    assert (await _run(p, ai=ai)).outcome == "fail"
    rows = await _rows(db_session, p.id)
    assert [(r.mode, r.outcome) for r in rows] == [("v1", "pass"), ("v1", "fail")]
    assert all(r.image_path and (gate_dir / r.image_path).is_file() for r in rows)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("verdict,outcome", [(False, "pass"), (True, "fail")])
async def test_gate_enforce_pending_once_then_terminal(
    real_gate, printer_factory, db_session, monkeypatch, verdict, outcome
):
    release = asyncio.Event()

    async def ask(url, model, jpeg, timeout):
        await release.wait()
        return AiVerdict(verdict, "r", 5, None)

    monkeypatch.setattr(gate, "ask_plate_empty", ask)
    p = await _printer(printer_factory)
    ai = SwapAiSettings(mode="enforce", url="http://x")
    for _ in range(3):
        assert (await _run(p, ai=ai, item_id=1)).outcome == "pending"
    assert [r.outcome for r in await _rows(db_session, p.id)] == ["pending"]
    release.set()
    await asyncio.sleep(0.05)
    assert (await _run(p, ai=ai, item_id=1)).outcome == outcome
    rows = await _rows(db_session, p.id)
    assert [r.outcome for r in rows] == ["pending", outcome]
    assert rows[1].mode == "enforce" and rows[1].ai_object is verdict and rows[1].image_path


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("verdict,outcome", [(True, "fail"), (None, "error")])
async def test_gate_shadow_ai_result_recorded_when_task_finishes(
    real_gate, printer_factory, db_session, monkeypatch, verdict, outcome
):
    async def ask(url, model, jpeg, timeout):
        return AiVerdict(verdict, "r", 5, None if verdict is not None else "boom")

    monkeypatch.setattr(gate, "ask_plate_empty", ask)
    p = await _printer(printer_factory)
    assert (await _run(p, ai=SwapAiSettings(mode="shadow", url="http://x"))).outcome == "pass"
    await asyncio.sleep(0.05)
    await gate.drain_records()
    rows = await _rows(db_session, p.id)
    ai_rows = [r for r in rows if r.ai_ms == 5]
    assert len(rows) == 2 and len(ai_rows) == 1
    assert (ai_rows[0].ai_object, ai_rows[0].outcome, ai_rows[0].mode) == (verdict, outcome, "shadow")
    assert ai_rows[0].image_path


@pytest.mark.asyncio
@pytest.mark.integration
async def test_gate_shadow_without_url_records_not_configured(real_gate, printer_factory, db_session):
    p = await _printer(printer_factory)
    await _run(p, ai=SwapAiSettings(mode="shadow", url=""))
    (row,) = await _rows(db_session, p.id)
    assert (row.mode, row.outcome, row.ai_reason) == ("v1", "pass", "not configured")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_orphan_images_are_swept(db_session, printer_factory, gate_dir):
    p = await printer_factory()
    gate_dir.mkdir(parents=True)
    old, young = gate_dir / "old.jpg", gate_dir / "young.jpg"
    old.write_bytes(b"x")
    young.write_bytes(b"x")
    os.utime(old, (time.time() - 3600,) * 2)
    await record_gate(p.id, mode="v1", outcome="pass", image=_frame())
    assert not old.exists() and young.exists()
    assert len(list(gate_dir.glob("*.jpg"))) == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_deleting_printer_removes_record_images(async_client, db_session, printer_factory, auth, gate_dir):
    pid = (await printer_factory()).id
    await record_gate(pid, mode="v1", outcome="pass", image=_frame())
    assert len(list(gate_dir.glob("*.jpg"))) == 1
    r = await async_client.delete(f"/api/v1/printers/{pid}", headers=auth)
    assert r.status_code == 200
    assert list(gate_dir.glob("*.jpg")) == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_settings_reject_enforce_without_url(async_client: AsyncClient, auth):
    r = await async_client.put("/api/v1/settings/", json={"swap_ai_mode": "enforce"}, headers=auth)
    assert r.status_code == 422 and "swap_ai_url" in r.json()["detail"]
    r = await async_client.put("/api/v1/settings/", json={"swap_ai_mode": "enforce", "swap_ai_url": "  "}, headers=auth)
    assert r.status_code == 422
    r = await async_client.put(
        "/api/v1/settings/", json={"swap_ai_mode": "enforce", "swap_ai_url": "http://10.0.0.5:11434"}, headers=auth
    )
    assert r.status_code == 200
    # stored enforce + URL: clearing only the URL is refused, switching mode away is fine
    r = await async_client.put("/api/v1/settings/", json={"swap_ai_url": ""}, headers=auth)
    assert r.status_code == 422
    r = await async_client.put("/api/v1/settings/", json={"swap_ai_mode": "shadow", "swap_ai_url": ""}, headers=auth)
    assert r.status_code == 200


@pytest.mark.asyncio
@pytest.mark.integration
async def test_print_start_clears_swap_ai_override(db_session, printer_factory, monkeypatch):
    from unittest.mock import AsyncMock

    from backend.app import main

    swap = await printer_factory(swap_enabled=True, swap_ai_override="swap.gate.aiObject")
    plain = await printer_factory(swap_ai_override="keep")
    swap_id, plain_id = swap.id, plain.id
    monkeypatch.setattr(main, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False))
    monkeypatch.setattr(main.ws_manager, "send_print_start", AsyncMock(side_effect=RuntimeError("stop here")))
    for pid in (swap_id, plain_id):
        with pytest.raises(RuntimeError):
            await main.on_print_start(pid, {})
    from backend.app.models.printer import Printer

    async def override(pid):
        db_session.expire_all()
        return (await db_session.execute(select(Printer.swap_ai_override).where(Printer.id == pid))).scalar_one()

    assert await override(swap_id) is None
    assert await override(plain_id) == "keep"
