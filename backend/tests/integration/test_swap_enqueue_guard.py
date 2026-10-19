"""Enqueue-time swap guard: stock-sliced files never enter a swap printer's queue."""

import json
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import func, select

from backend.app.core.config import settings as app_settings
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem

MARK = "; --- AUTO-CLEAR START ---"
GOOD = f"G28 Z P0\n{MARK}\nG1 Y265\n; EXECUTABLE_BLOCK_END\n"
STOCK = "G28\n; EXECUTABLE_BLOCK_END\n"


def _write(rel: str, gcode: str | None) -> None:
    path = Path(app_settings.base_dir) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    bbox = [118, 118, 138, 138]
    with zipfile.ZipFile(path, "w") as z:
        if gcode is None:  # unsliced project 3MF
            z.writestr("3D/3dmodel.model", "<model/>")
            return
        z.writestr("Metadata/plate_1.gcode", gcode)
        z.writestr(
            "Metadata/plate_1.json", json.dumps({"bbox_all": bbox, "bbox_objects": [{"name": "c", "bbox": bbox}]})
        )


@pytest.fixture
async def ctx(db_session, printer_factory, archive_factory):
    swap = await printer_factory(swap_enabled=True, swap_plates_remaining=3)
    plain = await printer_factory()
    files = {}
    for name, gcode in (("good", GOOD), ("stock", STOCK), ("unsliced", None)):
        rel = f"archive/enqueue_guard/{name}.gcode.3mf"
        _write(rel, gcode)
        arch = await archive_factory(swap.id, file_path=rel, filename=f"{name}.gcode.3mf")
        lib = LibraryFile(filename=f"{name}.gcode.3mf", file_path=rel, file_size=1, file_type="3mf")
        db_session.add(lib)
        await db_session.commit()
        await db_session.refresh(lib)
        files[name] = (arch, lib)
    yield swap, plain, files
    for name in files:
        (Path(app_settings.base_dir) / f"archive/enqueue_guard/{name}.gcode.3mf").unlink(missing_ok=True)


async def _count(db_session) -> int:
    db_session.expire_all()
    return (await db_session.execute(select(func.count()).select_from(PrintQueueItem))).scalar_one()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_stock_archive_rejected(async_client, db_session, ctx):
    swap, _plain, files = ctx
    r = await async_client.post("/api/v1/queue/", json={"printer_id": swap.id, "archive_id": files["stock"][0].id})
    assert r.status_code == 422
    assert r.json()["detail"].startswith("[swap.guard.noMarker]")
    assert await _count(db_session) == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_stock_library_file_rejected(async_client, db_session, ctx):
    swap, _plain, files = ctx
    r = await async_client.post("/api/v1/queue/", json={"printer_id": swap.id, "library_file_id": files["stock"][1].id})
    assert r.status_code == 422
    assert "[swap.guard.noMarker]" in r.json()["detail"]
    assert await _count(db_session) == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_swapper_sliced_accepted(async_client, ctx):
    swap, _plain, files = ctx
    r = await async_client.post("/api/v1/queue/", json={"printer_id": swap.id, "archive_id": files["good"][0].id})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_non_swap_printer_and_no_printer_unchanged(async_client, ctx):
    _swap, plain, files = ctx
    stock = files["stock"][0].id
    assert (
        await async_client.post("/api/v1/queue/", json={"printer_id": plain.id, "archive_id": stock})
    ).status_code == 200
    assert (await async_client.post("/api/v1/queue/", json={"archive_id": stock})).status_code == 200


@pytest.mark.asyncio
@pytest.mark.integration
async def test_unsliced_missing_and_raw_gcode_fail_closed(async_client, db_session, archive_factory, ctx):
    swap, _plain, files = ctx
    swap_id = swap.id
    r = await async_client.post("/api/v1/queue/", json={"printer_id": swap_id, "archive_id": files["unsliced"][0].id})
    assert r.status_code == 422 and r.json()["detail"].startswith("[swap.guard.unreadable]")
    missing = await archive_factory(swap_id, file_path="archive/enqueue_guard/nope.gcode.3mf")
    r = await async_client.post("/api/v1/queue/", json={"printer_id": swap_id, "archive_id": missing.id})
    assert r.status_code == 422 and r.json()["detail"].startswith("[swap.guard.unreadable]")
    raw_rel = "archive/enqueue_guard/raw.gcode.3mf"
    (Path(app_settings.base_dir) / raw_rel).write_text(GOOD)
    raw = await archive_factory(swap_id, file_path=raw_rel)
    r = await async_client.post("/api/v1/queue/", json={"printer_id": swap_id, "archive_id": raw.id})
    (Path(app_settings.base_dir) / raw_rel).unlink()
    assert r.status_code == 422 and r.json()["detail"].startswith("[swap.guard.unreadable]")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_schedule_only_patch_of_legacy_item_allowed(async_client, db_session, ctx):
    swap, _plain, files = ctx
    item = PrintQueueItem(printer_id=swap.id, archive_id=files["stock"][0].id, position=1, status="pending")
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)
    r = await async_client.patch(f"/api/v1/queue/{item.id}", json={"require_previous_success": True})
    assert r.status_code == 200, r.text
    r = await async_client.patch(f"/api/v1/queue/{item.id}", json={"printer_id": swap.id})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_invalid_config_fails_closed(async_client, db_session, ctx):
    swap, _plain, files = ctx
    swap.swap_config = "{not json"
    await db_session.commit()
    r = await async_client.post("/api/v1/queue/", json={"printer_id": swap.id, "archive_id": files["good"][0].id})
    assert r.status_code == 422
    assert r.json()["detail"].startswith("[swap.hold.configInvalid]")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_patch_to_swap_printer_rejected(async_client, db_session, ctx):
    swap, plain, files = ctx
    swap_id, plain_id = swap.id, plain.id
    created = await async_client.post(
        "/api/v1/queue/", json={"printer_id": plain.id, "archive_id": files["stock"][0].id}
    )
    item_id = created.json()["id"]
    r = await async_client.patch(f"/api/v1/queue/{item_id}", json={"printer_id": swap_id})
    assert r.status_code == 422
    assert "[swap.guard.noMarker]" in r.json()["detail"]
    r = await async_client.patch("/api/v1/queue/bulk", json={"item_ids": [item_id], "printer_id": swap_id})
    assert r.status_code == 422
    db_session.expire_all()
    item = (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()
    assert item.printer_id == plain_id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_library_add_to_queue_reports_rejection(async_client, db_session, ctx):
    swap, _plain, files = ctx
    r = await async_client.post(
        "/api/v1/library/files/add-to-queue",
        json={"file_ids": [files["stock"][1].id, files["good"][1].id], "printer_id": swap.id},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [a["file_id"] for a in body["added"]] == [files["good"][1].id]
    assert "[swap.guard.noMarker]" in body["errors"][0]["error"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_webhook_rejects_stock(async_client, db_session, ctx):
    from backend.app.core.auth import generate_api_key
    from backend.app.models.api_key import APIKey

    swap, _plain, files = ctx
    full, key_hash, prefix = generate_api_key()
    db_session.add(APIKey(name="k", key_hash=key_hash, key_prefix=prefix, can_queue=True))
    await db_session.commit()
    r = await async_client.post(
        "/api/v1/webhook/queue/add",
        json={"printer_id": swap.id, "archive_id": files["stock"][0].id},
        headers={"X-API-Key": full},
    )
    assert r.status_code == 422, r.text
    assert "[swap.guard.noMarker]" in r.json()["detail"]
