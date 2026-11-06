import pytest

from app.core.enums import (
    ApprovalDecision,
    ApprovalStatus,
    EventType,
    PlanStage,
    RiskLevel,
    TaskStatus,
    WorkflowStatus,
)
from app.core.exceptions import (
    ApprovalAlreadyDecidedError,
    ApprovalNotAuthorizedError,
    InvalidStateTransitionError,
)
from app.core.workflow import Task, TaskError, Workflow, fingerprint


def _task(key: str, seq: int, tool: str = "get_service_health", deps: list[str] | None = None):
    return Task(
        key=key,
        stage=PlanStage.INVESTIGATION,
        sequence=seq,
        description=f"task {key}",
        tool_name=tool,
        arguments={"service": "payment-service"},
        depends_on=deps or [],
    )


def _workflow_in(status: WorkflowStatus) -> Workflow:
    wf = Workflow.create("check payment service")
    path = {
        WorkflowStatus.EXECUTING: [
            WorkflowStatus.TRIAGING,
            WorkflowStatus.PLANNING,
            WorkflowStatus.EXECUTING,
        ],
        WorkflowStatus.AWAITING_APPROVAL: [
            WorkflowStatus.TRIAGING,
            WorkflowStatus.PLANNING,
            WorkflowStatus.EXECUTING,
            WorkflowStatus.AWAITING_APPROVAL,
        ],
    }[status]
    for step in path:
        wf.transition_to(step)
    return wf


def test_create_records_creation_event() -> None:
    wf = Workflow.create("hello")
    events = wf.drain_events()
    assert [e.event_type for e in events] == [EventType.WORKFLOW_CREATED]
    assert wf.pending_events == []


def test_transition_records_status_change_and_terminal_event() -> None:
    wf = Workflow.create("x")
    wf.drain_events()
    wf.transition_to(WorkflowStatus.TRIAGING)
    wf.fail("triage failed")
    types = [e.event_type for e in wf.drain_events()]
    assert types == [
        EventType.STATUS_CHANGED,
        EventType.STATUS_CHANGED,
        EventType.WORKFLOW_FAILED,
    ]
    assert wf.completed_at is not None
    assert wf.status_reason == "triage failed"


def test_invalid_transition_leaves_state_unchanged() -> None:
    wf = Workflow.create("x")
    with pytest.raises(InvalidStateTransitionError):
        wf.transition_to(WorkflowStatus.COMPLETED)
    assert wf.status == WorkflowStatus.RECEIVED


def test_next_task_respects_dependencies() -> None:
    wf = _workflow_in(WorkflowStatus.EXECUTING)
    wf.add_tasks([_task("I1", 1), _task("I2", 2, deps=["I1"])])
    first = wf.next_task(PlanStage.INVESTIGATION)
    assert first is not None and first.key == "I1"
    wf.start_task(first)
    assert wf.next_task(PlanStage.INVESTIGATION) is None
    wf.complete_task(first, {"ok": True})
    second = wf.next_task(PlanStage.INVESTIGATION)
    assert second is not None and second.key == "I2"


def test_failed_task_skips_transitive_dependents() -> None:
    wf = _workflow_in(WorkflowStatus.EXECUTING)
    wf.add_tasks([_task("I1", 1), _task("I2", 2, deps=["I1"]), _task("I3", 3, deps=["I2"])])
    t1 = wf.get_task("I1")
    wf.start_task(t1)
    wf.fail_task(t1, TaskError(code="boom", message="boom"))
    assert wf.get_task("I2").status == TaskStatus.SKIPPED
    assert wf.get_task("I3").status == TaskStatus.SKIPPED
    assert wf.stage_settled(PlanStage.INVESTIGATION)


def test_duplicate_task_keys_are_rejected() -> None:
    wf = Workflow.create("x")
    with pytest.raises(ValueError, match="Duplicate"):
        wf.add_tasks([_task("I1", 1), _task("I1", 2)])


def test_approval_is_bound_to_exact_arguments() -> None:
    wf = _workflow_in(WorkflowStatus.EXECUTING)
    task = _task("R1", 1, tool="restart_service")
    task.risk_level = RiskLevel.MEDIUM
    wf.add_tasks([task])
    approval = wf.request_approval(task, reasons=["medium risk"])
    assert task.status == TaskStatus.AWAITING_APPROVAL
    assert approval.arguments_hash == fingerprint("restart_service", task.arguments)
    assert approval.arguments_hash != fingerprint("restart_service", {"service": "auth-service"})


def test_approval_decision_records_identity_and_event() -> None:
    wf = _workflow_in(WorkflowStatus.EXECUTING)
    task = _task("R1", 1, tool="restart_service")
    wf.add_tasks([task])
    approval = wf.request_approval(task, reasons=["r"])
    wf.transition_to(WorkflowStatus.AWAITING_APPROVAL)
    wf.drain_events()
    wf.decide_approval(approval.id, ApprovalDecision.REJECT, decided_by="alice", comment="no")
    assert approval.status == ApprovalStatus.REJECTED
    assert approval.decided_by == "alice"
    assert approval.decided_at is not None
    assert [e.event_type for e in wf.drain_events()] == [EventType.APPROVAL_REJECTED]


@pytest.mark.parametrize("identity", ["", "  ", "system", "execution_agent", "Planning_Agent"])
def test_non_human_identities_cannot_approve(identity: str) -> None:
    wf = _workflow_in(WorkflowStatus.EXECUTING)
    task = _task("R1", 1, tool="restart_service")
    wf.add_tasks([task])
    approval = wf.request_approval(task, reasons=["r"])
    wf.transition_to(WorkflowStatus.AWAITING_APPROVAL)
    with pytest.raises(ApprovalNotAuthorizedError):
        wf.decide_approval(approval.id, ApprovalDecision.APPROVE, decided_by=identity)
    assert approval.status == ApprovalStatus.PENDING


def test_approval_cannot_be_decided_twice() -> None:
    wf = _workflow_in(WorkflowStatus.EXECUTING)
    task = _task("R1", 1, tool="restart_service")
    wf.add_tasks([task])
    approval = wf.request_approval(task, reasons=["r"])
    wf.transition_to(WorkflowStatus.AWAITING_APPROVAL)
    wf.decide_approval(approval.id, ApprovalDecision.APPROVE, decided_by="alice")
    with pytest.raises(ApprovalAlreadyDecidedError):
        wf.decide_approval(approval.id, ApprovalDecision.REJECT, decided_by="bob")
    assert approval.status == ApprovalStatus.APPROVED


def test_rejected_task_can_never_start() -> None:
    wf = _workflow_in(WorkflowStatus.EXECUTING)
    task = _task("R1", 1, tool="restart_service")
    wf.add_tasks([task])
    approval = wf.request_approval(task, reasons=["r"])
    wf.transition_to(WorkflowStatus.AWAITING_APPROVAL)
    wf.decide_approval(approval.id, ApprovalDecision.REJECT, decided_by="alice")
    wf.reject_task(task, approval)
    with pytest.raises(InvalidStateTransitionError):
        wf.start_task(task)


def test_cancel_cancels_pending_work_and_approvals() -> None:
    wf = _workflow_in(WorkflowStatus.EXECUTING)
    task = _task("R1", 1, tool="restart_service")
    other = _task("R2", 2)
    wf.add_tasks([task, other])
    approval = wf.request_approval(task, reasons=["r"])
    wf.transition_to(WorkflowStatus.AWAITING_APPROVAL)
    wf.cancel("user cancelled", component="api")
    assert wf.status == WorkflowStatus.CANCELLED
    assert approval.status == ApprovalStatus.CANCELLED
    assert task.status == TaskStatus.CANCELLED
    assert other.status == TaskStatus.CANCELLED
    with pytest.raises(InvalidStateTransitionError):
        wf.cancel("again", component="api")
