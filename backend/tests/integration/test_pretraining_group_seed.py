"""DCC: the 講習前 group (zero permissions) is seeded and idempotent."""

import pytest
from sqlalchemy import select

from backend.app.core import database as _database_module
from backend.app.core.database import seed_default_groups
from backend.app.models.group import Group


@pytest.mark.asyncio
async def test_pretraining_group_seeded_once(async_client):
    await seed_default_groups()
    await seed_default_groups()
    async with _database_module.async_session() as session:
        groups = (await session.execute(select(Group).where(Group.name == "講習前"))).scalars().all()
        assert len(groups) == 1
        assert not groups[0].permissions
        assert groups[0].is_system is False
