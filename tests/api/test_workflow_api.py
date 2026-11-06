import httpx
import pytest

from app.services.container import Container
from tests.fixtures.system import SCENARIO_1, SCENARIO_2, SCENARIO_3

ALICE = {"X-Approver-Token": "token-alice-123"}


async def _submit(client: httpx.AsyncClient, request: str, **headers: str) -> httpx.Response:
    return await client.post("/api/v1/workflows", json={"request": request}, headers=headers)


# ---------------------------------------------------------------------- submission
async def test_submit_returns_created_workflow(client: httpx.AsyncClient) -> None:
    response = await _submit(client, SCENARIO_2)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["category"] == "INCIDENT_INVESTIGATION"
    assert [t["tool"] for t in body["tasks"]][-1] == "generate_incident_report"
    assert body["summary"]["report"]["report_id"].startswith("INC-")
    assert "idempotency_key" not in str(body["tasks"][0])


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"request": ""},
        {"request": "    "},
        {"request": "x" * 2001},
        {"request": "abc\x00def"},
        {"request": "check auth service", "extra": 1},
    ],
)
async def test_invalid_submissions_are_rejected(client: httpx.AsyncClient, payload: dict) -> None:
    response = await client.post("/api/v1/workflows", json=payload)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert all(set(detail) == {"location", "message"} for detail in error["details"])


async def test_idempotency_key_returns_existing_workflow(client: httpx.AsyncClient) -> None:
    first = await _submit(client, SCENARIO_2, **{"Idempotency-Key": "abc-1"})
    second = await _submit(client, SCENARIO_2, **{"Idempotency-Key": "abc-1"})
    conflict = await _submit(client, SCENARIO_1, **{"Idempotency-Key": "abc-1"})
    assert (first.status_code, second.status_code, conflict.status_code) == (201, 200, 409)
    assert first.json()["id"] == second.json()["id"]
    assert conflict.json()["error"]["code"] == "idempotency_key_conflict"


async def test_invalid_idempotency_key_is_rejected(client: httpx.AsyncClient) -> None:
    response = await _submit(client, SCENARIO_2, **{"Idempotency-Key": "bad key with spaces"})
    assert response.status_code == 422


# ---------------------------------------------------------------------- reads
async def test_get_events_tasks_and_agent_runs(client: httpx.AsyncClient) -> None:
    workflow_id = (await _submit(client, SCENARIO_1)).json()["id"]
    detail = (await client.get(f"/api/v1/workflows/{workflow_id}")).json()
    assert detail["status"] == "AWAITING_APPROVAL"
    assert len(detail["pending_approval_ids"]) == 1

    events = (await client.get(f"/api/v1/workflows/{workflow_id}/events")).json()["events"]
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    assert events[0]["event_type"] == "WORKFLOW_CREATED"
    assert events[-1]["event_type"] == "STATUS_CHANGED"
    assert events[-1]["details"]["to_status"] == "AWAITING_APPROVAL"

    tasks = (await client.get(f"/api/v1/workflows/{workflow_id}/tasks")).json()
    assert [t["key"] for t in tasks] == ["I1", "I2", "I3", "R1"]
    assert tasks[3]["policy"]["verdict"] == "REQUIRE_APPROVAL"

    runs = (await client.get(f"/api/v1/workflows/{workflow_id}/agent-runs")).json()
    assert {r["agent"] for r in runs} >= {"triage_agent", "planning_agent", "policy_agent"}


@pytest.mark.parametrize("suffix", ["", "/events", "/tasks", "/agent-runs"])
async def test_unknown_workflow_is_404(client: httpx.AsyncClient, suffix: str) -> None:
    response = await client.get(f"/api/v1/workflows/does-not-exist{suffix}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "workflow_not_found"


async def test_list_paginates_and_filters(client: httpx.AsyncClient) -> None:
    await _submit(client, SCENARIO_2)
    await _submit(client, SCENARIO_1)
    await _submit(client, "Check the health of the auth service")

    page = (await client.get("/api/v1/workflows", params={"limit": 2})).json()
    assert page["total"] == 3 and len(page["items"]) == 2 and page["limit"] == 2
    rest = (await client.get("/api/v1/workflows", params={"limit": 2, "offset": 2})).json()
    assert len(rest["items"]) == 1
    all_ids = {i["id"] for i in page["items"]} | {i["id"] for i in rest["items"]}
    assert len(all_ids) == 3

    waiting = (await client.get("/api/v1/workflows", params={"status": "AWAITING_APPROVAL"})).json()
    assert waiting["total"] == 1
    bad = await client.get("/api/v1/workflows", params={"status": "SLEEPING"})
    assert bad.status_code == 422
    too_big = await client.get("/api/v1/workflows", params={"limit": 1000})
    assert too_big.status_code == 422


# ---------------------------------------------------------------------- approvals
async def test_full_approval_flow_over_http(
    client: httpx.AsyncClient, container: Container
) -> None:
    workflow_id = (await _submit(client, SCENARIO_1)).json()["id"]
    pending = (await client.get("/api/v1/approvals/pending")).json()
    assert len(pending) == 1 and pending[0]["tool"] == "restart_service"

    response = await client.post(
        f"/api/v1/approvals/{pending[0]['id']}/decision",
        json={"decision": "approve", "comment": "go ahead"},
        headers=ALICE,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["approval"]["status"] == "APPROVED"
    assert body["approval"]["decided_by"] == "alice"
    assert body["workflow"]["status"] == "COMPLETED"
    assert body["workflow"]["id"] == workflow_id
    assert container.environment.mutation_count("restart_service") == 1

    again = await client.post(
        f"/api/v1/approvals/{pending[0]['id']}/decision",
        json={"decision": "approve"},
        headers=ALICE,
    )
    assert again.status_code == 409
    assert container.environment.mutation_count("restart_service") == 1


async def test_rejection_over_http(client: httpx.AsyncClient, container: Container) -> None:
    await _submit(client, SCENARIO_3)
    approval = (await client.get("/api/v1/approvals/pending")).json()[0]
    response = await client.post(
        f"/api/v1/approvals/{approval['id']}/decision",
        json={"decision": "reject", "comment": "not during peak hours"},
        headers=ALICE,
    )
    workflow = response.json()["workflow"]
    assert workflow["status"] == "CANCELLED"
    assert workflow["tasks"][-1]["status"] == "REJECTED"
    assert container.environment.mutation_count() == 0


@pytest.mark.parametrize("headers", [{}, {"X-Approver-Token": "wrong"}])
async def test_decision_requires_valid_token(client: httpx.AsyncClient, headers: dict) -> None:
    await _submit(client, SCENARIO_1)
    approval = (await client.get("/api/v1/approvals/pending")).json()[0]
    response = await client.post(
        f"/api/v1/approvals/{approval['id']}/decision",
        json={"decision": "approve"},
        headers=headers,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "approval_not_authorized"


async def test_decision_body_cannot_set_identity(client: httpx.AsyncClient) -> None:
    await _submit(client, SCENARIO_1)
    approval = (await client.get("/api/v1/approvals/pending")).json()[0]
    response = await client.post(
        f"/api/v1/approvals/{approval['id']}/decision",
        json={"decision": "approve", "decided_by": "ceo"},
        headers=ALICE,
    )
    assert response.status_code == 422


async def test_invalid_decision_value(client: httpx.AsyncClient) -> None:
    await _submit(client, SCENARIO_1)
    approval = (await client.get("/api/v1/approvals/pending")).json()[0]
    response = await client.post(
        f"/api/v1/approvals/{approval['id']}/decision", json={"decision": "maybe"}, headers=ALICE
    )
    assert response.status_code == 422


async def test_unknown_approval_is_404(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/approvals/nope/decision", json={"decision": "approve"}, headers=ALICE
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------- cancel and resume
async def test_cancel_and_invalid_transitions(client: httpx.AsyncClient) -> None:
    workflow_id = (await _submit(client, SCENARIO_1)).json()["id"]
    resume_waiting = await client.post(f"/api/v1/workflows/{workflow_id}/resume")
    assert resume_waiting.status_code == 409
    assert resume_waiting.json()["error"]["code"] == "workflow_not_resumable"

    cancelled = await client.post(
        f"/api/v1/workflows/{workflow_id}/cancel", json={"reason": "duplicate incident"}
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    assert cancelled.json()["status_reason"] == "duplicate incident"

    twice = await client.post(f"/api/v1/workflows/{workflow_id}/cancel")
    assert twice.status_code == 409
    assert twice.json()["error"]["code"] == "invalid_state_transition"
    resume = await client.post(f"/api/v1/workflows/{workflow_id}/resume")
    assert resume.status_code == 409
    assert (await client.get("/api/v1/approvals/pending")).json() == []


async def test_resume_after_decision_without_resume(client: httpx.AsyncClient) -> None:
    workflow_id = (await _submit(client, SCENARIO_1)).json()["id"]
    approval = (await client.get("/api/v1/approvals/pending")).json()[0]
    decided = await client.post(
        f"/api/v1/approvals/{approval['id']}/decision",
        json={"decision": "approve", "resume": False},
        headers=ALICE,
    )
    assert decided.json()["workflow"]["status"] == "AWAITING_APPROVAL"
    resumed = await client.post(f"/api/v1/workflows/{workflow_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "COMPLETED"


# ---------------------------------------------------------------------- system
async def test_tools_and_agents(client: httpx.AsyncClient) -> None:
    tools = {t["name"]: t for t in (await client.get("/api/v1/tools")).json()}
    assert tools["restart_service"]["risk_level"] == "MEDIUM"
    assert tools["purge_service_logs"]["risk_level"] == "CRITICAL"
    assert "input_schema" in tools["get_service_metrics"]
    agents = [a["name"] for a in (await client.get("/api/v1/agents")).json()]
    assert agents == [
        "orchestrator",
        "triage_agent",
        "planning_agent",
        "policy_agent",
        "execution_agent",
        "verification_agent",
    ]


async def test_simulation_reset(client: httpx.AsyncClient, container: Container) -> None:
    workflow_id = (await _submit(client, SCENARIO_1)).json()["id"]
    approval = (await client.get("/api/v1/approvals/pending")).json()[0]
    await client.post(
        f"/api/v1/approvals/{approval['id']}/decision", json={"decision": "approve"}, headers=ALICE
    )
    assert container.environment.services["payment-service"].fault == "NONE"
    reset = await client.post("/api/v1/simulation/reset")
    assert reset.json()["services"]["payment-service"] == "CONNECTION_LEAK"
    assert workflow_id
