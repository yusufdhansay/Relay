from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.database import Database
from app.services.container import Container
from tests.fixtures.system import build_container


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'relay-test.db'}")
    await db.create_schema()
    yield db
    await db.dispose()


@pytest.fixture
async def container(tmp_path: Path) -> AsyncIterator[Container]:
    c = await build_container(tmp_path / "relay.db")
    yield c
    await c.shutdown()
