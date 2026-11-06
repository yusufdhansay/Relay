from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.database import Database


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'relay-test.db'}")
    await db.create_schema()
    yield db
    await db.dispose()
