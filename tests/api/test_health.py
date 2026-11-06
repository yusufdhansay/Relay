import httpx


async def test_health_reports_components(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["llm_provider"] == "mock"
    assert body["version"]


async def test_openapi_lists_all_endpoints(client: httpx.AsyncClient) -> None:
    paths = set((await client.get("/openapi.json")).json()["paths"])
    assert {
        "/health",
        "/api/v1/workflows",
        "/api/v1/workflows/{workflow_id}",
        "/api/v1/workflows/{workflow_id}/events",
        "/api/v1/workflows/{workflow_id}/tasks",
        "/api/v1/workflows/{workflow_id}/cancel",
        "/api/v1/workflows/{workflow_id}/resume",
        "/api/v1/approvals/pending",
        "/api/v1/approvals/{approval_id}/decision",
        "/api/v1/tools",
        "/api/v1/agents",
    } <= paths
