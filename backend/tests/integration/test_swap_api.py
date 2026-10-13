"""Swap REST endpoints: status, settings, plate count, resume."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.core.auth import get_password_hash
from backend.app.models.group import Group
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.services.printer_manager import printer_manager

PW = "Whatever1!"


async def _token(async_client: AsyncClient, db_session, username: str, permissions: list[str]) -> dict:
    group = Group(name=f"grp-{username}", description="t", permissions=permissions, is_system=False)
    db_session.add(group)
    await db_session.flush()
    db_session.add(
        User(username=username, password_hash=get_password_hash(PW), role="user", is_active=True, groups=[group])
    )
    await db_session.commit()
    r = await async_client.post("/api/v1/auth/login", json={"username": username, "password": PW})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
async def auth(async_client: AsyncClient):
    await async_client.post(
        "/api/v1/auth/setup",
        json={"auth_enabled": True, "admin_username": "swapadmin", "admin_password": "AdminPass1!"},
    )
    r = await async_client.post("/api/v1/auth/login", json={"username": "swapadmin", "password": "AdminPass1!"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _db_printer(db_session, pid: int) -> Printer:
    db_session.expire_all()
    return (await db_session.execute(select(Printer).where(Printer.id == pid))).scalar_one()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_swap_requires_read(async_client, db_session, printer_factory, auth):
    p = await printer_factory()
    only_clear = await _token(async_client, db_session, "clearer", ["printers:clear_plate"])
    reader = await _token(async_client, db_session, "reader", ["printers:read"])
    url = f"/api/v1/printers/{p.id}/swap"
    assert (await async_client.get(url, headers=only_clear)).status_code == 403
    r = await async_client.get(url, headers=reader)
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False and body["plates_remaining"] is None and body["hold_reason"] is None
    assert body["config"]["exclude_front_mm"] == 20.0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_put_swap_requires_update(async_client, db_session, printer_factory, auth):
    p = await printer_factory()
    only_clear = await _token(async_client, db_session, "clearer", ["printers:clear_plate"])
    updater = await _token(async_client, db_session, "updater", ["printers:update"])
    url = f"/api/v1/printers/{p.id}/swap"
    assert (await async_client.put(url, json={"enabled": True}, headers=only_clear)).status_code == 403
    r = await async_client.put(url, json={"enabled": True, "config": {"exclude_front_mm": 5}}, headers=updater)
    assert r.status_code == 200
    assert r.json()["enabled"] is True and r.json()["config"]["exclude_front_mm"] == 5
    # config null resets to defaults; absent config leaves it alone
    r = await async_client.put(url, json={"enabled": True}, headers=updater)
    assert r.json()["config"]["exclude_front_mm"] == 5
    r = await async_client.put(url, json={"config": None}, headers=updater)
    assert r.json()["config"]["exclude_front_mm"] == 20.0
    assert (await _db_printer(db_session, p.id)).swap_config is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_put_rejects_invalid_config(async_client, printer_factory, auth):
    p = await printer_factory()
    r = await async_client.put(f"/api/v1/printers/{p.id}/swap", json={"config": {"exclude_front_mm": -1}}, headers=auth)
    assert r.status_code == 422


@pytest.mark.asyncio
@pytest.mark.integration
async def test_disabling_clears_hold(async_client, printer_factory, auth):
    p = await printer_factory(swap_enabled=True, swap_hold_reason="swap.gate.noPlate", swap_hold_detail="x")
    r = await async_client.put(f"/api/v1/printers/{p.id}/swap", json={"enabled": False}, headers=auth)
    assert r.json()["hold_reason"] is None and r.json()["hold_detail"] is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_plates_clears_out_of_plates_hold(async_client, printer_factory, auth):
    p = await printer_factory(swap_enabled=True, swap_plates_remaining=0, swap_hold_reason="swap.hold.outOfPlates")
    r = await async_client.post(f"/api/v1/printers/{p.id}/swap/plates", json={"remaining": 5}, headers=auth)
    assert r.status_code == 200
    assert r.json()["plates_remaining"] == 5 and r.json()["hold_reason"] is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_plates_keeps_camera_hold(async_client, printer_factory, auth):
    p = await printer_factory(swap_enabled=True, swap_plates_remaining=0, swap_hold_reason="swap.gate.noPlate")
    r = await async_client.post(f"/api/v1/printers/{p.id}/swap/plates", json={"remaining": 5}, headers=auth)
    assert r.json()["plates_remaining"] == 5 and r.json()["hold_reason"] == "swap.gate.noPlate"


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("n", [-1, 100])
async def test_plates_range(async_client, printer_factory, auth, n):
    p = await printer_factory()
    r = await async_client.post(f"/api/v1/printers/{p.id}/swap/plates", json={"remaining": n}, headers=auth)
    assert r.status_code == 422


@pytest.mark.asyncio
@pytest.mark.integration
async def test_resume_clears_hold_and_requires_clear_plate(async_client, db_session, printer_factory, auth):
    p = await printer_factory(swap_enabled=True, swap_plates_remaining=3, swap_hold_reason="swap.gate.noPlate")
    reader = await _token(async_client, db_session, "reader", ["printers:read", "printers:update"])
    url = f"/api/v1/printers/{p.id}/swap/resume"
    assert (await async_client.post(url, headers=reader)).status_code == 403
    r = await async_client.post(url, headers=auth)
    assert r.status_code == 200 and r.json()["hold_reason"] is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_unknown_printer_404(async_client, auth):
    base = "/api/v1/printers/99999/swap"
    assert (await async_client.get(base, headers=auth)).status_code == 404
    assert (await async_client.put(base, json={"enabled": True}, headers=auth)).status_code == 404
    assert (await async_client.post(base + "/plates", json={"remaining": 1}, headers=auth)).status_code == 404
    assert (await async_client.post(base + "/resume", headers=auth)).status_code == 404


@pytest.mark.asyncio
@pytest.mark.integration
async def test_api_updates_memory_mirror(async_client, printer_factory, auth):
    p = await printer_factory(swap_enabled=True, swap_plates_remaining=3, swap_hold_reason="swap.gate.noPlate")
    printer_manager._swap_state_loaded = True
    await printer_manager._refresh_swap_state(p.id)
    assert printer_manager.is_swap_blocked(p.id) == "swap.gate.noPlate"
    r = await async_client.post(f"/api/v1/printers/{p.id}/swap/resume", headers=auth)
    assert r.status_code == 200
    await printer_manager._refresh_swap_state(p.id)
    assert printer_manager.is_swap_blocked(p.id) is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_rest_status_carries_swap_state(async_client, printer_factory):
    """The 30 s REST poll replaces the cached status; without ``swap`` the card panel would vanish."""
    p = await printer_factory(swap_enabled=True, swap_plates_remaining=2, swap_hold_reason="swap.gate.noPlate")
    printer_manager._swap_state_loaded = True
    await printer_manager._refresh_swap_state(p.id)
    r = await async_client.get(f"/api/v1/printers/{p.id}/status")
    assert r.status_code == 200
    assert r.json()["swap"] == {"enabled": True, "plates_remaining": 2, "hold_reason": "swap.gate.noPlate"}


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("state", ["RUNNING", "PAUSE", "PREPARE"])
async def test_enable_refused_while_printing(async_client, db_session, printer_factory, auth, state):
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    p = await printer_factory()
    with patch.object(printer_manager, "get_status", MagicMock(return_value=SimpleNamespace(state=state))):
        r = await async_client.put(f"/api/v1/printers/{p.id}/swap", json={"enabled": True}, headers=auth)
    assert r.status_code == 409
    assert (await _db_printer(db_session, p.id)).swap_enabled is False


@pytest.mark.asyncio
@pytest.mark.integration
async def test_enable_refused_while_queue_item_printing(async_client, db_session, printer_factory, auth):
    from backend.app.models.print_queue import PrintQueueItem

    p = await printer_factory()
    db_session.add(PrintQueueItem(printer_id=p.id, status="printing"))
    await db_session.commit()
    r = await async_client.put(f"/api/v1/printers/{p.id}/swap", json={"enabled": True}, headers=auth)
    assert r.status_code == 409
    assert (await _db_printer(db_session, p.id)).swap_enabled is False


@pytest.mark.asyncio
@pytest.mark.integration
async def test_enable_resets_plate_count_and_blocks(async_client, db_session, printer_factory, auth):
    p = await printer_factory(swap_enabled=False, swap_plates_remaining=4)
    r = await async_client.put(f"/api/v1/printers/{p.id}/swap", json={"enabled": True}, headers=auth)
    assert r.status_code == 200
    assert r.json()["plates_remaining"] is None
    await printer_manager._refresh_swap_state(p.id)
    assert printer_manager.is_swap_blocked(p.id) == "swap.hold.platesUnknown"
    # Re-sending enabled=True on an already-enabled printer keeps the count.
    await async_client.post(f"/api/v1/printers/{p.id}/swap/plates", json={"remaining": 2}, headers=auth)
    r = await async_client.put(f"/api/v1/printers/{p.id}/swap", json={"enabled": True}, headers=auth)
    assert r.json()["plates_remaining"] == 2


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "home-axes?axes=all"),
        ("post", "bed-jog?distance=5"),
        ("post", "xy-jog?y=10"),
        ("post", "calibration?bed_leveling=true"),
    ],
)
async def test_motion_refused_on_swap_printer(async_client, printer_factory, auth, method, path):
    from unittest.mock import MagicMock, patch

    p = await printer_factory(swap_enabled=True, swap_plates_remaining=3)
    client = MagicMock()
    with patch.object(printer_manager, "get_client", MagicMock(return_value=client)):
        r = await getattr(async_client, method)(f"/api/v1/printers/{p.id}/{path}", headers=auth)
    assert r.status_code == 409
    assert r.json()["detail"].startswith("[swap.control.motionBlocked]")
    client.send_gcode.assert_not_called()
    client.start_calibration.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_motion_allowed_on_normal_printer(async_client, printer_factory, auth):
    from unittest.mock import MagicMock, patch

    p = await printer_factory()
    client = MagicMock()
    client.send_gcode.return_value = True
    with patch.object(printer_manager, "get_client", MagicMock(return_value=client)):
        r = await async_client.post(f"/api/v1/printers/{p.id}/home-axes?axes=all", headers=auth)
    assert r.status_code == 200
    client.send_gcode.assert_called_once_with("G28")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_reference_mutations_require_printers_update(async_client, db_session, printer_factory, auth):
    p = await printer_factory()
    viewer = await _token(async_client, db_session, "viewer", ["camera:view"])
    base = f"/api/v1/printers/{p.id}/camera/plate-detection"
    assert (await async_client.post(f"{base}/calibrate", headers=viewer)).status_code == 403
    assert (await async_client.delete(f"{base}/calibrate", headers=viewer)).status_code == 403
    assert (await async_client.put(f"{base}/references/0?label=x", headers=viewer)).status_code == 403
    assert (await async_client.delete(f"{base}/references/0", headers=viewer)).status_code == 403
    # Reading stays camera:view.
    assert (await async_client.get(f"{base}/references", headers=viewer)).status_code != 403
