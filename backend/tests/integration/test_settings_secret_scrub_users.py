"""GET /settings returns secrets only to callers who could also change them."""

import pytest
from httpx import AsyncClient

from backend.app.core.auth import generate_api_key, get_password_hash
from backend.app.models.api_key import APIKey
from backend.app.models.group import Group
from backend.app.models.settings import Settings
from backend.app.models.user import User

PW = "Whatever1!"
# Names listed apart from values to avoid secret-scanner false positives.
SECRET_KEYS = ["mqtt_password", "ha_token", "prometheus_token", "virtual_printer_access_code", "obico_ml_token"]


async def _seed_secrets(db_session):
    for key in SECRET_KEYS:
        db_session.add(Settings(key=key, value="testdata"))
    await db_session.commit()


async def _enable_auth(async_client: AsyncClient):
    await async_client.post(
        "/api/v1/auth/setup",
        json={"auth_enabled": True, "admin_username": "scrubadmin", "admin_password": "AdminPass1!"},
    )


async def _headers(async_client: AsyncClient, db_session, username: str, permissions: list[str]) -> dict:
    group = Group(name=f"grp-{username}", description="t", permissions=permissions, is_system=False)
    db_session.add(group)
    await db_session.flush()
    db_session.add(
        User(username=username, password_hash=get_password_hash(PW), role="user", is_active=True, groups=[group])
    )
    await db_session.commit()
    r = await async_client.post("/api/v1/auth/login", json={"username": username, "password": PW})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_read_only_user_gets_blanked_secrets(async_client, db_session):
    await _seed_secrets(db_session)
    await _enable_auth(async_client)
    h = await _headers(async_client, db_session, "reader", ["settings:read"])
    resp = await async_client.get("/api/v1/settings/", headers=h)
    assert resp.status_code == 200
    for key in SECRET_KEYS:
        assert resp.json()[key] == "", key


@pytest.mark.asyncio
@pytest.mark.integration
async def test_updater_user_gets_secrets(async_client, db_session):
    await _seed_secrets(db_session)
    await _enable_auth(async_client)
    h = await _headers(async_client, db_session, "updater", ["settings:read", "settings:update"])
    resp = await async_client.get("/api/v1/settings/", headers=h)
    assert resp.status_code == 200
    for key in SECRET_KEYS:
        assert resp.json()[key] == "testdata", key


@pytest.mark.asyncio
@pytest.mark.integration
async def test_auth_disabled_returns_secrets(async_client, db_session):
    await _seed_secrets(db_session)
    resp = await async_client.get("/api/v1/settings/")
    assert resp.status_code == 200
    for key in SECRET_KEYS:
        assert resp.json()[key] == "testdata", key


@pytest.mark.asyncio
@pytest.mark.integration
async def test_api_key_still_blanked(async_client, db_session):
    await _seed_secrets(db_session)
    full_key, key_hash, key_prefix = generate_api_key()
    db_session.add(APIKey(name="k", key_hash=key_hash, key_prefix=key_prefix, can_read_status=True, enabled=True))
    await db_session.commit()
    resp = await async_client.get("/api/v1/settings/", headers={"X-API-Key": full_key})
    assert resp.status_code == 200
    for key in SECRET_KEYS:
        assert resp.json()[key] == "", key
