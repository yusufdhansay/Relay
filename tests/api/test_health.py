import httpx

from app.core.config import Settings
from app.main import create_app


async def test_health_returns_ok_and_version() -> None:
    app = create_app(Settings(environment="test"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]
