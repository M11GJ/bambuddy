"""Real names from DCC Login: OIDC sets User.display_name, UIs show the label."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.library import LibraryFile
from backend.app.models.user import User
from backend.tests.integration.test_oidc_relogin import _make_rsa_key, _trigger_oidc_callback

ISSUER = "https://idp.display-name.example.com"
CLIENT = "display-name-client"


async def _setup_provider(async_client: AsyncClient) -> int:
    await async_client.post(
        "/api/v1/auth/setup",
        json={"auth_enabled": True, "admin_username": "dnadmin", "admin_password": "AdminPass1!"},
    )
    login = await async_client.post("/api/v1/auth/login", json={"username": "dnadmin", "password": "AdminPass1!"})
    resp = await async_client.post(
        "/api/v1/auth/oidc/providers",
        json={
            "name": "DNIdP",
            "issuer_url": ISSUER,
            "client_id": CLIENT,
            "client_secret": "s",
            "scopes": "openid email profile",
            "is_enabled": True,
            "auto_create_users": True,
        },
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_oidc_sets_updates_and_keeps_display_name(async_client: AsyncClient, db_session):
    pid = await _setup_provider(async_client)
    pem, jwks = _make_rsa_key()

    async def login(**claims):
        await _trigger_oidc_callback(
            async_client,
            db_session,
            pid,
            ISSUER,
            CLIENT,
            pem,
            jwks,
            sub="dn-sub-1",
            email="yamada@example.com",
            extra_claims=claims,
        )
        db_session.expire_all()
        return (await db_session.execute(select(User).where(User.email == "yamada@example.com"))).scalar_one()

    user = await login(name="  山田太郎  ")
    assert user.display_name == "山田太郎"
    username = user.username

    user = await login(name="山田 太郎")  # updated on a later login
    assert user.display_name == "山田 太郎"

    user = await login(name="   ")  # blank leaves it unchanged
    assert user.display_name == "山田 太郎"
    user = await login()  # missing too
    assert user.display_name == "山田 太郎"
    assert user.username == username


@pytest.mark.asyncio
@pytest.mark.integration
async def test_library_and_users_show_label(async_client: AsyncClient, db_session):
    named = User(username="yamada_t", display_name="山田太郎", password_hash="x", role="user")
    plain = User(username="plainuser", password_hash="x", role="user")
    db_session.add_all([named, plain])
    await db_session.flush()
    files = [
        LibraryFile(filename=f"{u.username}.3mf", file_path=f"/t/{u.username}", file_size=1, file_type="3mf", created_by_id=u.id)
        for u in (named, plain)
    ]
    db_session.add_all(files)
    await db_session.commit()

    for f, expected in zip(files, ["山田太郎", "plainuser"], strict=True):
        resp = await async_client.get(f"/api/v1/library/files/{f.id}")
        assert resp.json()["created_by_username"] == expected

    users = (await async_client.get("/api/v1/users/slim")).json()
    assert {u["username"] for u in users} >= {"山田太郎", "plainuser"}
