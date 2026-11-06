import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.core.enums import (
    ApprovalDecision,
    EventType,
    PlanStage,
    RiskLevel,
    TaskStatus,
    WorkflowStatus,
)
from app.core.exceptions import ConcurrencyConflictError, WorkflowNotFoundError
from app.core.workflow import AgentRun, Task, Workflow
from app.database import Base, Database
from app.repositories import WorkflowRepository


def _build_workflow() -> Workflow:
    wf = Workflow.create("restart payment", idempotency_key="key-1", request_hash="h")
    for status in (WorkflowStatus.TRIAGING, WorkflowStatus.PLANNING, WorkflowStatus.EXECUTING):
        wf.transition_to(status)
    wf.triage = {"category": "INCIDENT_INVESTIGATION"}
    wf.add_tasks(
        [
            Task(
                key="I1",
                stage=PlanStage.INVESTIGATION,
                sequence=1,
                description="health",
                tool_name="get_service_health",
                arguments={"service": "payment-service"},
            ),
            Task(
                key="R1",
                stage=PlanStage.REMEDIATION,
                sequence=2,
                description="restart",
                tool_name="restart_service",
                arguments={"service": "payment-service", "reason": "leak"},
                risk_level=RiskLevel.MEDIUM,
                requires_approval=True,
            ),
        ]
    )
    return wf


async def test_round_trip_preserves_aggregate(database: Database) -> None:
    wf = _build_workflow()
    approval = wf.request_approval(wf.get_task("R1"), reasons=["medium risk"])
    async with database.session() as session:
        await WorkflowRepository(session).save(wf)

    async with database.session() as session:
        loaded = await WorkflowRepository(session).get(wf.id)

    assert loaded.status == WorkflowStatus.EXECUTING
    assert loaded.version == 1
    assert loaded.triage == {"category": "INCIDENT_INVESTIGATION"}
    assert [t.key for t in loaded.tasks] == ["I1", "R1"]
    assert loaded.get_task("R1").status == TaskStatus.AWAITING_APPROVAL
    assert loaded.approvals[0].id == approval.id
    assert loaded.approvals[0].arguments_hash == approval.arguments_hash
    assert loaded.created_at.tzinfo is not None


async def test_events_are_appended_in_order(database: Database) -> None:
    wf = _build_workflow()
    async with database.session() as session:
        await WorkflowRepository(session).save(wf)

    async with database.session() as session:
        repo = WorkflowRepository(session)
        loaded = await repo.get(wf.id)
        loaded.transition_to(WorkflowStatus.VERIFYING)
        await repo.save(loaded)

    async with database.session() as session:
        events = await WorkflowRepository(session).list_events(wf.id)

    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    assert events[0].event_type == EventType.WORKFLOW_CREATED
    last = events[-1]
    assert last.event_type == EventType.STATUS_CHANGED
    assert last.details == {"from_status": "EXECUTING", "to_status": "VERIFYING", "reason": None}


async def test_stale_write_is_rejected(database: Database) -> None:
    wf = _build_workflow()
    async with database.session() as session:
        await WorkflowRepository(session).save(wf)

    async with database.session() as session:
        first = await WorkflowRepository(session).get(wf.id)
    async with database.session() as session:
        second = await WorkflowRepository(session).get(wf.id)

    first.transition_to(WorkflowStatus.VERIFYING)
    async with database.session() as session:
        await WorkflowRepository(session).save(first)

    second.cancel("late cancel", component="api")
    with pytest.raises(ConcurrencyConflictError):
        async with database.session() as session:
            await WorkflowRepository(session).save(second)

    async with database.session() as session:
        stored = await WorkflowRepository(session).get(wf.id)
    assert stored.status == WorkflowStatus.VERIFYING


async def test_failed_save_rolls_back_events(database: Database) -> None:
    wf = _build_workflow()
    async with database.session() as session:
        await WorkflowRepository(session).save(wf)
    async with database.session() as session:
        stale = await WorkflowRepository(session).get(wf.id)
        stale.version = 99
        stale.transition_to(WorkflowStatus.VERIFYING)
        with pytest.raises(ConcurrencyConflictError):
            await WorkflowRepository(session).save(stale)
    async with database.session() as session:
        events = await WorkflowRepository(session).list_events(wf.id)
    assert all(e.details.get("to_status") != "VERIFYING" for e in events)


async def test_unknown_workflow_raises(database: Database) -> None:
    async with database.session() as session:
        with pytest.raises(WorkflowNotFoundError):
            await WorkflowRepository(session).get("missing")


async def test_list_filters_and_paginates(database: Database) -> None:
    async with database.session() as session:
        repo = WorkflowRepository(session)
        for i in range(5):
            wf = Workflow.create(f"request {i}")
            if i % 2 == 0:
                wf.transition_to(WorkflowStatus.TRIAGING)
            await repo.save(wf)
            await asyncio.sleep(0.001)

    async with database.session() as session:
        repo = WorkflowRepository(session)
        page, total = await repo.list(limit=2, offset=0)
        assert total == 5
        assert [w.request for w in page] == ["request 4", "request 3"]
        triaging, count = await repo.list(status=WorkflowStatus.TRIAGING)
        assert count == 3
        assert {w.status for w in triaging} == {WorkflowStatus.TRIAGING}


async def test_pending_approvals_and_lookup(database: Database) -> None:
    wf = _build_workflow()
    approval = wf.request_approval(wf.get_task("R1"), reasons=["r"])
    wf.transition_to(WorkflowStatus.AWAITING_APPROVAL)
    async with database.session() as session:
        await WorkflowRepository(session).save(wf)

    async with database.session() as session:
        repo = WorkflowRepository(session)
        pending = await repo.list_pending_approvals()
        assert [a.id for a in pending] == [approval.id]
        assert await repo.workflow_id_for_approval(approval.id) == wf.id
        loaded = await repo.get(wf.id)
        loaded.decide_approval(approval.id, ApprovalDecision.APPROVE, decided_by="alice")
        await repo.save(loaded)

    async with database.session() as session:
        assert await WorkflowRepository(session).list_pending_approvals() == []


async def test_idempotency_key_lookup(database: Database) -> None:
    wf = _build_workflow()
    async with database.session() as session:
        await WorkflowRepository(session).save(wf)
    async with database.session() as session:
        repo = WorkflowRepository(session)
        found = await repo.find_by_idempotency_key("key-1")
        assert found is not None and found.id == wf.id
        assert await repo.find_by_idempotency_key("other") is None


async def test_agent_runs_are_recorded(database: Database) -> None:
    wf = _build_workflow()
    async with database.session() as session:
        repo = WorkflowRepository(session)
        await repo.save(wf)
        await repo.add_agent_runs(
            [
                AgentRun(
                    workflow_id=wf.id,
                    agent="triage_agent",
                    operation="triage",
                    provider="mock",
                    model="mock-1",
                    success=True,
                    llm_calls=1,
                    latency_ms=1.5,
                    prompt_tokens=10,
                    completion_tokens=5,
                )
            ]
        )
    async with database.session() as session:
        runs = await WorkflowRepository(session).list_agent_runs(wf.id)
    assert len(runs) == 1
    assert runs[0].prompt_tokens == 10


def test_alembic_migration_matches_models(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'migrated.db'}"
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.attributes["database_url"] = url
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")

    async def inspect_columns() -> dict[str, set[str]]:
        db = Database(url)
        async with db.engine.connect() as conn:
            result = await conn.run_sync(
                lambda sync: {
                    table: {c["name"] for c in inspect(sync).get_columns(table)}
                    for table in inspect(sync).get_table_names()
                    if table != "alembic_version"
                }
            )
        await db.dispose()
        return result

    migrated = asyncio.run(inspect_columns())
    expected = {
        name: {column.name for column in table.columns}
        for name, table in Base.metadata.tables.items()
    }
    assert migrated == expected
