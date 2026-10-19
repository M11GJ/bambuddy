"""display_name must be added to an EXISTING users table (production DB copy)."""

import importlib
import pkgutil

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import backend.app.models as models_pkg
from backend.app.core.database import Base, run_migrations

# run_migrations ALTERs tables from many model modules; load them all.
for _m in pkgutil.iter_modules(models_pkg.__path__):
    importlib.import_module(f"backend.app.models.{_m.name}")


@pytest.fixture(autouse=True)
def force_sqlite_dialect(monkeypatch):
    from backend.app.core import database as database_module, db_dialect

    monkeypatch.setattr(db_dialect, "is_sqlite", lambda: True)
    monkeypatch.setattr(db_dialect, "is_postgres", lambda: False)
    monkeypatch.setattr(database_module, "is_sqlite", lambda: True)


@pytest.mark.asyncio
async def test_display_name_added_to_existing_users_table(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Rebuild users in its pre-feature shape, with a real row.
            await conn.execute(text("ALTER TABLE users DROP COLUMN display_name"))
            await conn.execute(
                text("INSERT INTO users (username, email, role, auth_source, is_active) VALUES ('yamada_t','y@x.jp','user','oidc',1)")
            )
            cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(users)"))).all()}
            assert "display_name" not in cols

            await run_migrations(conn)
            await run_migrations(conn)  # idempotent

            cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(users)"))).all()}
            row = (await conn.execute(text("SELECT username, email, display_name FROM users"))).one()
        assert "display_name" in cols
        assert tuple(row) == ("yamada_t", "y@x.jp", None)
    finally:
        await engine.dispose()
