from pathlib import Path

import pytest

from app.core.enums import ApprovalDecision, ApprovalStatus, TaskStatus, WorkflowStatus
from app.core.exceptions import (
    ApprovalAlreadyDecidedError,
    ApprovalNotAuthorizedError,
    ApprovalNotFoundError,
    InvalidStateTransitionError,
)
from app.services.container import Container
from tests.fixtures.system import SCENARIO_1, SCENARIO_3, build_container

ALICE = "token-alice-123"
BOB = "token-bob-456"


async def _waiting(container: Container, request: str = SCENARIO_1):
    workflow = (await container.workflows.submit(request)).workflow
    assert workflow.status == WorkflowStatus.AWAITING_APPROVAL
    return workflow, workflow.pending_approvals()[0]


async def test_pending_lists_waiting_approvals(container: Container) -> None:
    workflow, approval = await _waiting(container)
    pending = await container.approvals.pending()
    assert [a.id for a in pending] == [approval.id]
    assert pending[0].workflow_id == workflow.id
    assert pending[0].reasons


async def test_identity_comes_from_token(container: Container) -> None:
    _, approval = await _waiting(container)
    result = await container.approvals.decide(approval.id, ApprovalDecision.APPROVE, token=BOB)
    assert result.approval.decided_by == "bob"
    assert result.approval.decided_at is not None
    assert result.workflow.status == WorkflowStatus.COMPLETED
    assert await container.approvals.pending() == []


@pytest.mark.parametrize("token", [None, "", "token-alice", "TOKEN-ALICE-123", "alice"])
async def test_invalid_tokens_cannot_decide(container: Container, token: str | None) -> None:
    workflow, approval = await _waiting(container)
    with pytest.raises(ApprovalNotAuthorizedError):
        await container.approvals.decide(approval.id, ApprovalDecision.APPROVE, token=token)
    stored = await container.workflows.get(workflow.id)
    assert stored.approvals[0].status == ApprovalStatus.PENDING
    assert container.environment.mutation_count() == 0


async def test_no_configured_approvers_means_no_decisions(tmp_path: Path) -> None:
    container = await build_container(tmp_path / "n.db", approver_tokens=None)
    _, approval = await _waiting(container)
    with pytest.raises(ApprovalNotAuthorizedError, match="No approvers"):
        await container.approvals.decide(approval.id, ApprovalDecision.APPROVE, token=ALICE)
    await container.shutdown()


async def test_decision_is_final(container: Container) -> None:
    _, approval = await _waiting(container)
    await container.approvals.decide(
        approval.id, ApprovalDecision.REJECT, token=ALICE, resume=False
    )
    with pytest.raises(ApprovalAlreadyDecidedError):
        await container.approvals.decide(approval.id, ApprovalDecision.APPROVE, token=BOB)


async def test_unknown_approval(container: Container) -> None:
    with pytest.raises(ApprovalNotFoundError):
        await container.approvals.decide("nope", ApprovalDecision.APPROVE, token=ALICE)


async def test_decide_without_resume_then_resume(container: Container) -> None:
    workflow, approval = await _waiting(container)
    result = await container.approvals.decide(
        approval.id, ApprovalDecision.APPROVE, token=ALICE, resume=False
    )
    assert result.workflow.status == WorkflowStatus.AWAITING_APPROVAL
    assert container.environment.mutation_count() == 0
    resumed = await container.workflows.resume(workflow.id)
    assert resumed.status == WorkflowStatus.COMPLETED
    assert container.environment.mutation_count("restart_service") == 1


async def test_rejection_cancels_and_records_comment(container: Container) -> None:
    _, approval = await _waiting(container, SCENARIO_3)
    result = await container.approvals.decide(
        approval.id, ApprovalDecision.REJECT, token=ALICE, comment="change freeze"
    )
    assert result.workflow.status == WorkflowStatus.CANCELLED
    assert result.workflow.get_task("R1").status == TaskStatus.REJECTED
    assert result.workflow.approvals[0].comment == "change freeze"
    assert "update_service_config" not in container.environment.calls


async def test_cannot_decide_on_cancelled_workflow(container: Container) -> None:
    workflow, approval = await _waiting(container)
    await container.workflows.cancel(workflow.id)
    with pytest.raises((ApprovalAlreadyDecidedError, InvalidStateTransitionError)):
        await container.approvals.decide(approval.id, ApprovalDecision.APPROVE, token=ALICE)
    assert container.environment.mutation_count() == 0
