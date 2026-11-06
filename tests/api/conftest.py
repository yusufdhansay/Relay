from collections.abc import AsyncIterator

import httpx
import pytest

from app.main import create_app
from app.services.container import Container


@pytest.fixture
async def client(container: Container) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(container=container)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://relay.test") as c:
            yield c
