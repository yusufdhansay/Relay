import asyncio
from pathlib import Path

import pytest

from app.core.enums import (
    ApprovalDecision,
    ApprovalStatus,
    TaskStatus,
    WorkflowStatus,
)
from app.core.exceptions import (
    DuplicateRequestConflictError,
    LLMProviderError,
    WorkflowBusyError,
    WorkflowNotResumableError,
)
from app.repositories import WorkflowRepository
from app.services.container import Container
from app.tools.operations import FailureKind, SimulatedEnvironment
from tests.fixtures.providers import ScriptedProvider
from tests.fixtures.system import (
    SCENARIO_1,
    SCENARIO_2,
    SCENARIO_3,
    build_container,
    decide_directly,
    event_types,
)


def _statuses(workflow) -> dict[str, str]:
    return {t.key: t.status.value for t in workflow.tasks}


# ---------------------------------------------------------------------- scenario 1
async def test_scenario_one_pauses_for_approval_then_resolves(container: Container) -> None:
    env = container.environment
    workflow = (await container.workflows.submit(SCENARIO_1)).workflow

    assert workflow.status == WorkflowStatus.AWAITING_APPROVAL
    assert _statuses(workflow) == {
        "I1": "SUCCEEDED",
        "I2": "SUCCEEDED",
        "I3": "SUCCEEDED",
        "R1": "AWAITING_APPROVAL",
    }
    assert workflow.diagnosis and workflow.diagnosis["fault_found"]
    assert env.mutation_count() == 0, "nothing may change before approval"
    approval = workflow.pending_approvals()[0]
    assert approval.tool_name == "restart_service"
    assert approval.risk_level == "HIGH"

    done = await decide_directly(container, workflow, ApprovalDecision.APPROVE, by="alice")

    assert done.status == WorkflowStatus.COMPLETED
    assert env.mutation_count("restart_service") == 1
    assert done.verification["objective_achieved"] is True
    assert done.verification["probes"][0]["status"] == "HEALTHY"
    assert done.summary["actions"][0]["decided_by"] == "alice"
    assert "approved by alice" in done.summary["headline"]

    types = await event_types(container, workflow.id)
    order = [
        "WORKFLOW_CREATED",
        "REQUEST_CLASSIFIED",
        "PLAN_GENERATED",
        "POLICY_EVALUATED",
        "TOOL_EXECUTED",
        "DIAGNOSIS_RECORDED",
        "APPROVAL_REQUESTED",
        "APPROVAL_GRANTED",
        "WORKFLOW_RESUMED",
        "VERIFICATION_COMPLETED",
        "WORKFLOW_COMPLETED",
    ]
    positions = [types.index(t) for t in order]
    assert positions == sorted(positions)

    runs = await container.workflows.agent_runs(workflow.id)
    agents = {r.agent for r in runs}
    assert agents == {
        "triage_agent",
        "planning_agent",
        "policy_agent",
        "verification_agent",
    }
    assert all(r.success for r in runs)


# ---------------------------------------------------------------------- scenario 2
async def test_scenario_two_investigates_read_only_and_writes_report(
    container: Container,
) -> None:
    env = container.environment
    workflow = (await container.workflows.submit(SCENARIO_2)).workflow

    assert workflow.status == WorkflowStatus.COMPLETED
    assert workflow.triage["constraints"]["read_only"] is True
    assert [t.tool_name for t in workflow.tasks] == [
        "get_service_health",
        "get_service_metrics",
        "search_service_logs",
        "generate_incident_report",
    ]
    assert env.mutation_count() == 0
    assert not workflow.approvals
    checks = {c["name"]: c["passed"] for c in workflow.verification["checks"]}
    assert checks["read_only_respected"] and checks["report_generated"]
    report = workflow.summary["report"]
    assert "connection pool exhaustion" in report["markdown"]
    assert "No changes were made" in report["markdown"]
    runs = await container.workflows.agent_runs(workflow.id)
    assert any(r.agent == "execution_agent" and r.operation == "resolve_arguments" for r in runs)


# ---------------------------------------------------------------------- scenario 3
async def test_scenario_three_rejection_never_executes_change(container: Container) -> None:
    env = container.environment
    workflow = (await container.workflows.submit(SCENARIO_3)).workflow

    assert workflow.status == WorkflowStatus.AWAITING_APPROVAL
    task = workflow.get_task("R1")
    assert task.tool_name == "update_service_config"
    assert task.arguments["changes"] == {"db_pool_max_size": 40}

    done = await decide_directly(
        container, workflow, ApprovalDecision.REJECT, by="bob", comment="change freeze"
    )

    assert done.status == WorkflowStatus.CANCELLED
    assert "rejected by bob" in done.status_reason
    assert done.get_task("R1").status == TaskStatus.REJECTED
    assert "update_service_config" not in env.calls
    assert env.services["payment-service"].config["db_pool_max_size"] == 10
    events = await container.workflows.events(workflow.id)
    rejected = [e for e in events if e.event_type == "APPROVAL_REJECTED"]
    assert rejected[0].details["decided_by"] == "bob"
    assert rejected[0].details["comment"] == "change freeze"
    assert "TOOL_EXECUTED" not in [
        e.event_type for e in events if e.details.get("tool") == "update_service_config"
    ]


async def test_approved_config_change_mitigates_and_verifies(container: Container) -> None:
    workflow = (await container.workflows.submit(SCENARIO_3)).workflow
    done = await decide_directly(container, workflow, ApprovalDecision.APPROVE)
    assert done.status == WorkflowStatus.COMPLETED
    assert container.environment.services["payment-service"].config["db_pool_max_size"] == 40


# ---------------------------------------------------------------------- failures
async def test_unsupported_request_fails_at_triage(container: Container) -> None:
    workflow = (await container.workflows.submit("Write a haiku about autumn leaves")).workflow
    assert workflow.status == WorkflowStatus.FAILED
    assert "cannot be handled" in workflow.status_reason
    assert workflow.tasks == []


async def test_transient_tool_failures_recover_within_task(container: Container) -> None:
    container.environment.inject_failure("get_service_metrics", FailureKind.TRANSIENT, times=2)
    workflow = (await container.workflows.submit(SCENARIO_2)).workflow
    assert workflow.status == WorkflowStatus.COMPLETED
    assert workflow.get_task("I2").attempts == 1
    types = await event_types(container, workflow.id)
    assert types.count("RETRY_ATTEMPTED") == 2


async def test_exhausted_retries_trigger_one_workflow_retry(container: Container) -> None:
    container.environment.inject_failure("get_service_metrics", FailureKind.TRANSIENT, times=3)
    workflow = (await container.workflows.submit(SCENARIO_2)).workflow
    assert workflow.status == WorkflowStatus.COMPLETED
    assert workflow.retry_count == 1
    assert "WORKFLOW_RETRY" in await event_types(container, workflow.id)


async def test_permanent_tool_failure_escalates(container: Container) -> None:
    container.environment.inject_failure("search_service_logs", FailureKind.PERMANENT)
    workflow = (await container.workflows.submit(SCENARIO_2)).workflow
    assert workflow.status == WorkflowStatus.FAILED
    assert workflow.get_task("I3").status == TaskStatus.FAILED
    assert workflow.get_task("I4").status == TaskStatus.SKIPPED
    assert workflow.verification["recommendation"] == "ESCALATE"
    assert workflow.retry_count == 0


async def test_verification_failure_when_restart_does_not_fix(tmp_path: Path) -> None:
    env = SimulatedEnvironment()
    env.services["payment-service"].fault_persistent = True
    container = await build_container(tmp_path / "r.db", env=env)
    workflow = (await container.workflows.submit(SCENARIO_1)).workflow
    done = await decide_directly(container, workflow, ApprovalDecision.APPROVE)
    assert done.status == WorkflowStatus.FAILED
    assert env.mutation_count("restart_service") == 1
    assert "service_healthy:payment-service" in done.status_reason
    assert done.verification["objective_achieved"] is False
    await container.shutdown()


async def test_planner_outage_fails_workflow_with_clear_reason(tmp_path: Path) -> None:
    llm = ScriptedProvider({"plan_investigation": [LLMProviderError("provider down")]})
    container = await build_container(tmp_path / "p.db", llm=llm)
    workflow = (await container.workflows.submit(SCENARIO_1)).workflow
    assert workflow.status == WorkflowStatus.FAILED
    assert "planning_agent could not complete" in workflow.status_reason
    assert "AGENT_FAILED" in await event_types(container, workflow.id)
    await container.shutdown()


async def test_step_budget_bounds_the_loop(tmp_path: Path) -> None:
    container = await build_container(tmp_path / "b.db", max_steps_per_run=3)
    workflow = (await container.workflows.submit(SCENARIO_2)).workflow
    assert workflow.status == WorkflowStatus.FAILED
    assert "Step budget" in workflow.status_reason
    await container.shutdown()


# ---------------------------------------------------------------------- persistence
async def test_state_survives_a_new_process(tmp_path: Path) -> None:
    first = await build_container(tmp_path / "s.db")
    workflow = (await first.workflows.submit(SCENARIO_1)).workflow
    await first.shutdown()

    second = await build_container(tmp_path / "s.db", env=first.environment)
    loaded = await second.workflows.get(workflow.id)
    assert loaded.status == WorkflowStatus.AWAITING_APPROVAL
    assert _statuses(loaded) == _statuses(workflow)
    done = await decide_directly(second, loaded, ApprovalDecision.APPROVE)
    assert done.status == WorkflowStatus.COMPLETED
    await second.shutdown()


async def test_crash_recovery_replays_instead_of_repeating(tmp_path: Path) -> None:
    container = await build_container(tmp_path / "c.db")
    env = container.environment
    workflow = (await container.workflows.submit(SCENARIO_1)).workflow
    approval = workflow.pending_approvals()[0]

    # Simulate a crash: the restart ran, but the process died before recording the result.
    async with container.database.session() as session:
        repo = WorkflowRepository(session)
        wf = await repo.get(workflow.id)
        wf.decide_approval(approval.id, ApprovalDecision.APPROVE, decided_by="alice")
        wf.transition_to(WorkflowStatus.EXECUTING)
        task = wf.get_task("R1")
        task.resolved_arguments = task.arguments
        wf.start_task(task)
        await repo.save(wf)
    env.restart("payment-service", task.arguments["reason"], task.idempotency_key)

    done = await container.workflows.resume(workflow.id)
    assert done.status == WorkflowStatus.COMPLETED
    assert env.services["payment-service"].restart_count == 1
    assert done.get_task("R1").output["replayed"] is True
    assert "WORKFLOW_RECOVERED" in await event_types(container, workflow.id)
    await container.shutdown()


# ---------------------------------------------------------------------- control
async def test_cancel_while_waiting_for_approval(container: Container) -> None:
    workflow = (await container.workflows.submit(SCENARIO_1)).workflow
    cancelled = await container.workflows.cancel(workflow.id, reason="no longer needed")
    assert cancelled.status == WorkflowStatus.CANCELLED
    assert cancelled.approvals[0].status == ApprovalStatus.CANCELLED
    assert cancelled.get_task("R1").status == TaskStatus.CANCELLED
    with pytest.raises(WorkflowNotResumableError):
        await container.workflows.resume(workflow.id)
    assert container.environment.mutation_count() == 0


async def test_resume_while_waiting_is_rejected(container: Container) -> None:
    workflow = (await container.workflows.submit(SCENARIO_1)).workflow
    with pytest.raises(WorkflowNotResumableError, match="waiting for an approval"):
        await container.workflows.resume(workflow.id)


async def test_concurrent_processing_is_rejected(container: Container) -> None:
    workflow = (await container.workflows.submit(SCENARIO_1)).workflow
    async with container.locks.hold(workflow.id):
        with pytest.raises(WorkflowBusyError):
            await container.workflows.cancel(workflow.id)


async def test_idempotent_submission(container: Container) -> None:
    first = await container.workflows.submit(SCENARIO_2, idempotency_key="req-1")
    second = await container.workflows.submit(SCENARIO_2, idempotency_key="req-1")
    assert first.created and not second.created
    assert first.workflow.id == second.workflow.id
    assert container.environment.calls["generate_incident_report"] == 1
    with pytest.raises(DuplicateRequestConflictError):
        await container.workflows.submit(SCENARIO_1, idempotency_key="req-1")
    assert "DUPLICATE_REQUEST" in await event_types(container, first.workflow.id)


async def test_concurrent_duplicate_submissions_create_one_workflow(container: Container) -> None:
    results = await asyncio.gather(
        container.workflows.submit(SCENARIO_2, idempotency_key="race"),
        container.workflows.submit(SCENARIO_2, idempotency_key="race"),
        return_exceptions=True,
    )
    workflows = [r.workflow.id for r in results if not isinstance(r, BaseException)]
    assert len(set(workflows)) == 1
    _, total = await container.workflows.list(status=None, limit=10, offset=0)
    assert total == 1
