from app.core.enums import ApprovalDecision, PlanStage, RiskLevel, WorkflowStatus
from app.core.workflow import Task
from app.tools.operations import FailureKind
from tests.fixtures.builders import (
    build_components,
    executing_workflow,
    investigation_task,
    run_task_directly,
)
from tests.fixtures.providers import ScriptedProvider

TRIAGE = {"constraints": {"read_only": False}, "services": ["payment-service"]}
RESTART_ARGS = {"service": "payment-service", "reason": "release leaked connections"}


def _restart_task(c) -> Task:
    return Task(
        key="R1",
        stage=PlanStage.REMEDIATION,
        sequence=4,
        description="restart",
        tool_name="restart_service",
        arguments=c.registry.normalise_arguments("restart_service", RESTART_ARGS),
        risk_level=RiskLevel.HIGH,
        requires_approval=True,
    )


def _approve(wf, task, who="alice"):
    approval = wf.request_approval(task, reasons=["risk"])
    wf.transition_to(WorkflowStatus.AWAITING_APPROVAL)
    wf.decide_approval(approval.id, ApprovalDecision.APPROVE, decided_by=who)
    wf.transition_to(WorkflowStatus.EXECUTING)
    return approval


async def test_transient_failures_are_retried_with_backoff() -> None:
    c = build_components()
    c.env.inject_failure("get_service_metrics", FailureKind.TRANSIENT, times=2)
    wf = executing_workflow("x", TRIAGE)
    task = investigation_task("I1", 1, "get_service_metrics", {"service": "payment-service"})
    wf.add_tasks([task])
    outcome = await c.executor.execute(
        wf, task, c.registry.normalise_arguments(task.tool_name, task.arguments)
    )
    assert outcome.success
    assert [a.attempt for a in outcome.attempts] == [1, 2]
    assert c.sleeps == [0.2, 0.4]
    assert c.env.calls["get_service_metrics"] == 3


async def test_retries_are_bounded() -> None:
    c = build_components()
    c.env.inject_failure("get_service_health", FailureKind.TRANSIENT, times=10)
    wf = executing_workflow("x", TRIAGE)
    task = investigation_task("I1", 1, "get_service_health", {})
    wf.add_tasks([task])
    outcome = await c.executor.execute(wf, task, {"service": None})
    assert not outcome.success and outcome.retryable
    assert len(outcome.attempts) == 3
    assert c.env.calls["get_service_health"] == 3


async def test_permanent_failures_are_not_retried() -> None:
    c = build_components()
    c.env.inject_failure("get_service_health", FailureKind.PERMANENT)
    wf = executing_workflow("x", TRIAGE)
    task = investigation_task("I1", 1, "get_service_health", {})
    wf.add_tasks([task])
    outcome = await c.executor.execute(wf, task, {"service": None})
    assert not outcome.success and not outcome.retryable
    assert c.env.calls["get_service_health"] == 1


async def test_unapproved_change_never_reaches_the_tool() -> None:
    c = build_components()
    wf = executing_workflow("x", TRIAGE)
    task = _restart_task(c)
    wf.add_tasks([task])
    outcome = await c.executor.execute(wf, task, task.arguments)
    assert not outcome.success
    assert outcome.error_code == "execution_not_authorized"
    assert c.env.mutation_count() == 0
    assert "restart_service" not in c.env.calls


async def test_approved_change_runs_once() -> None:
    c = build_components()
    wf = executing_workflow("x", TRIAGE)
    task = _restart_task(c)
    wf.add_tasks([task])
    _approve(wf, task)
    outcome = await c.executor.execute(wf, task, task.arguments)
    assert outcome.success and outcome.permit_basis == "human_approval"
    assert c.env.mutation_count("restart_service") == 1


async def test_arguments_cannot_change_after_approval() -> None:
    c = build_components()
    wf = executing_workflow("x", TRIAGE)
    task = _restart_task(c)
    wf.add_tasks([task])
    _approve(wf, task)
    tampered = {**task.arguments, "service": "auth-service"}
    outcome = await c.executor.execute(wf, task, tampered)
    assert not outcome.success
    assert "differ from the approved" in (outcome.error or "")
    assert c.env.mutation_count() == 0


async def test_timeout_after_side_effect_does_not_duplicate_restart() -> None:
    c = build_components()
    c.env.hang_seconds = 5
    c.env.inject_failure("restart_service", FailureKind.TIMEOUT_AFTER_EFFECT)
    c.executor.timeout_seconds = 0.05
    wf = executing_workflow("x", TRIAGE)
    task = _restart_task(c)
    wf.add_tasks([task])
    _approve(wf, task)
    outcome = await c.executor.execute(wf, task, task.arguments)
    assert outcome.success
    assert outcome.output["replayed"] is True
    assert outcome.attempts[0].error_code == "tool_timeout"
    assert c.env.mutation_count("restart_service") == 1
    assert c.env.services["payment-service"].restart_count == 1


async def test_argument_resolution_fills_service_from_health_output() -> None:
    c = build_components()
    wf = executing_workflow("Investigate the service", {**TRIAGE, "services": []})
    health = investigation_task("I1", 1, "get_service_health", {})
    logs = investigation_task("I2", 2, "search_service_logs", {"min_severity": "ERROR"})
    logs.depends_on = logs.input_from = ["I1"]
    wf.add_tasks([health, logs])
    await run_task_directly(c, wf, health)
    result = await c.executor.resolve_arguments(wf, logs)
    assert result.output["service"] == "payment-service"
    assert result.output["min_severity"] == "ERROR"
    assert result.runs[0].success


async def test_argument_resolution_cannot_override_planned_arguments() -> None:
    llm = ScriptedProvider(
        {
            "resolve_arguments": [
                {
                    "arguments": {"service": "auth-service", "min_severity": "DEBUG"},
                    "rationale": "x" * 5,
                },
                {
                    "arguments": {"service": "auth-service", "min_severity": "DEBUG"},
                    "rationale": "x" * 5,
                },
            ]
        }
    )
    c = build_components(llm)
    wf = executing_workflow("x", TRIAGE)
    health = investigation_task("I1", 1, "get_service_health", {})
    logs = investigation_task("I2", 2, "search_service_logs", {"min_severity": "ERROR"})
    logs.depends_on = logs.input_from = ["I1"]
    wf.add_tasks([health, logs])
    await run_task_directly(c, wf, health)
    try:
        await c.executor.resolve_arguments(wf, logs)
    except Exception as exc:
        assert "must stay" in str(exc)
    else:
        raise AssertionError("resolution that changes planned arguments must fail")
