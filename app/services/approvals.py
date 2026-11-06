"""Human approval service.

Approver identity comes from a secret token configured in ``RELAY_APPROVER_TOKENS``, never
from the request body. Agents have no token and no code path to this service, so an LLM
cannot approve its own actions.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from app.core.config import Settings
from app.core.enums import ApprovalDecision
from app.core.exceptions import ApprovalNotAuthorizedError, ApprovalNotFoundError
from app.core.locks import WorkflowLocks
from app.core.logging import get_logger
from app.core.orchestrator import Orchestrator
from app.core.workflow import Approval, Workflow
from app.database import Database
from app.repositories import WorkflowRepository

logger = get_logger("approvals")


@dataclass
class DecisionResult:
    approval: Approval
    workflow: Workflow


class ApprovalService:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        orchestrator: Orchestrator,
        locks: WorkflowLocks,
    ) -> None:
        self.settings = settings
        self.database = database
        self.orchestrator = orchestrator
        self.locks = locks

    def authenticate(self, token: str | None) -> str:
        """Return the approver name for a token, comparing in constant time."""
        approvers = self.settings.approvers()
        if not approvers:
            raise ApprovalNotAuthorizedError("No approvers are configured on this server")
        if not token:
            raise ApprovalNotAuthorizedError("An approver token is required")
        match = None
        for known, name in approvers.items():
            if hmac.compare_digest(known.encode(), token.encode()):
                match = name
        if match is None:
            logger.warning("approval.auth_failed")
            raise ApprovalNotAuthorizedError("Approver token is not valid")
        return match

    async def pending(self) -> list[Approval]:
        async with self.database.session() as session:
            return await WorkflowRepository(session).list_pending_approvals()

    async def decide(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        *,
        token: str | None,
        comment: str | None = None,
        resume: bool = True,
    ) -> DecisionResult:
        approver = self.authenticate(token)
        async with self.database.session() as session:
            workflow_id = await WorkflowRepository(session).workflow_id_for_approval(approval_id)
        if workflow_id is None:
            raise ApprovalNotFoundError(f"Approval {approval_id} not found")

        async with self.locks.hold(workflow_id), self.database.session() as session:
            repo = WorkflowRepository(session)
            workflow = await repo.get(workflow_id)
            approval = workflow.decide_approval(
                approval_id, decision, decided_by=approver, comment=comment
            )
            await repo.save(workflow)
        logger.info(
            "approval.decided",
            extra={
                "fields": {
                    "approval_id": approval_id,
                    "workflow_id": workflow_id,
                    "decision": decision.value,
                    "decided_by": approver,
                }
            },
        )
        if resume:
            workflow = await self.orchestrator.resume(workflow_id)
        return DecisionResult(approval=approval, workflow=workflow)
