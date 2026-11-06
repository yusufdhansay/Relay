"""Persistence for the workflow aggregate and its audit events.

``save`` uses optimistic concurrency: the workflow row is updated only if its stored
version matches the version that was loaded. Two writers can never silently overwrite
each other.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.enums import ApprovalStatus, WorkflowStatus
from app.core.exceptions import ConcurrencyConflictError, WorkflowNotFoundError
from app.core.workflow import (
    AgentRun,
    Approval,
    Task,
    TaskError,
    Workflow,
    WorkflowEvent,
    new_id,
    utc_now,
)
from app.models import AgentRunRecord, ApprovalRecord, EventRecord, TaskRecord, WorkflowRecord


class WorkflowRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ reads
    async def get(self, workflow_id: str) -> Workflow:
        record = await self.session.scalar(
            select(WorkflowRecord)
            .where(WorkflowRecord.id == workflow_id)
            .options(selectinload(WorkflowRecord.tasks), selectinload(WorkflowRecord.approvals))
            .execution_options(populate_existing=True)
        )
        if record is None:
            raise WorkflowNotFoundError(f"Workflow {workflow_id} not found")
        return _to_domain(record, record.tasks, record.approvals)

    async def exists(self, workflow_id: str) -> bool:
        found = await self.session.scalar(
            select(WorkflowRecord.id).where(WorkflowRecord.id == workflow_id)
        )
        return found is not None

    async def find_by_idempotency_key(self, key: str) -> Workflow | None:
        workflow_id = await self.session.scalar(
            select(WorkflowRecord.id).where(WorkflowRecord.idempotency_key == key)
        )
        return await self.get(workflow_id) if workflow_id else None

    async def list(
        self,
        *,
        status: WorkflowStatus | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Workflow], int]:
        """Return a page of workflows (without tasks and approvals) and the total count."""
        query = select(WorkflowRecord)
        count_query = select(func.count()).select_from(WorkflowRecord)
        if status is not None:
            query = query.where(WorkflowRecord.status == status.value)
            count_query = count_query.where(WorkflowRecord.status == status.value)
        query = (
            query.order_by(WorkflowRecord.created_at.desc(), WorkflowRecord.id)
            .limit(limit)
            .offset(offset)
        )
        records = (await self.session.scalars(query)).all()
        total = await self.session.scalar(count_query) or 0
        return [_to_domain(r, [], []) for r in records], total

    async def list_events(self, workflow_id: str) -> list[WorkflowEvent]:
        records = await self.session.scalars(
            select(EventRecord)
            .where(EventRecord.workflow_id == workflow_id)
            .order_by(EventRecord.sequence)
        )
        return [
            WorkflowEvent(
                id=r.id,
                workflow_id=r.workflow_id,
                sequence=r.sequence,
                event_type=r.event_type,
                component=r.component,
                timestamp=r.timestamp,
                details=r.details,
            )
            for r in records
        ]

    async def list_pending_approvals(self) -> list[Approval]:
        records = await self.session.scalars(
            select(ApprovalRecord)
            .where(ApprovalRecord.status == ApprovalStatus.PENDING.value)
            .order_by(ApprovalRecord.requested_at)
        )
        return [_approval_to_domain(r) for r in records]

    async def workflow_id_for_approval(self, approval_id: str) -> str | None:
        return await self.session.scalar(
            select(ApprovalRecord.workflow_id).where(ApprovalRecord.id == approval_id)
        )

    async def list_unfinished_ids(self) -> list[str]:
        """Workflows interrupted mid-run (not terminal and not waiting for a human)."""
        active = [
            WorkflowStatus.RECEIVED,
            WorkflowStatus.TRIAGING,
            WorkflowStatus.PLANNING,
            WorkflowStatus.EXECUTING,
            WorkflowStatus.VERIFYING,
        ]
        rows = await self.session.scalars(
            select(WorkflowRecord.id).where(WorkflowRecord.status.in_([s.value for s in active]))
        )
        return list(rows)

    async def list_agent_runs(self, workflow_id: str) -> list[AgentRun]:
        records = await self.session.scalars(
            select(AgentRunRecord)
            .where(AgentRunRecord.workflow_id == workflow_id)
            .order_by(AgentRunRecord.created_at)
        )
        return [
            AgentRun.model_validate(
                {c.name: getattr(r, c.name) for c in AgentRunRecord.__table__.columns}
            )
            for r in records
        ]

    # ------------------------------------------------------------------ writes
    async def save(self, workflow: Workflow) -> None:
        """Persist the aggregate and append its pending events in the current transaction."""
        values = _workflow_values(workflow)
        if workflow.version == 0:
            self.session.add(WorkflowRecord(id=workflow.id, version=1, **values))
            await self.session.flush()
        else:
            result = await self.session.execute(
                update(WorkflowRecord)
                .where(WorkflowRecord.id == workflow.id, WorkflowRecord.version == workflow.version)
                .values(version=workflow.version + 1, **values)
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                raise ConcurrencyConflictError(
                    f"Workflow {workflow.id} was modified by another operation"
                )
        workflow.version += 1

        for task in workflow.tasks:
            await self.session.merge(TaskRecord(workflow_id=workflow.id, **_task_values(task)))
        await self.session.flush()
        for approval in workflow.approvals:
            await self.session.merge(ApprovalRecord(**approval.model_dump()))

        events = workflow.drain_events()
        if events:
            last = await self.session.scalar(
                select(func.max(EventRecord.sequence)).where(EventRecord.workflow_id == workflow.id)
            )
            sequence = last or 0
            for event in events:
                sequence += 1
                self.session.add(
                    EventRecord(
                        id=new_id(),
                        workflow_id=workflow.id,
                        sequence=sequence,
                        event_type=event.event_type.value,
                        component=event.component,
                        timestamp=event.timestamp,
                        details=event.details,
                    )
                )
        await self.session.flush()

    async def add_agent_runs(self, runs: list[AgentRun]) -> None:
        for run in runs:
            self.session.add(AgentRunRecord(**run.model_dump()))
        await self.session.flush()


# ---------------------------------------------------------------------- mapping
def _workflow_values(workflow: Workflow) -> dict[str, Any]:
    category = workflow.triage.get("category") if workflow.triage else None
    return {
        "request": workflow.request,
        "status": workflow.status.value,
        "status_reason": workflow.status_reason,
        "stage": workflow.stage.value,
        "remediation_planned": workflow.remediation_planned,
        "category": category,
        "triage": workflow.triage,
        "diagnosis": workflow.diagnosis,
        "verification": workflow.verification,
        "summary": workflow.summary,
        "retry_count": workflow.retry_count,
        "idempotency_key": workflow.idempotency_key,
        "request_hash": workflow.request_hash,
        "created_at": workflow.created_at,
        "updated_at": utc_now(),
        "completed_at": workflow.completed_at,
    }


def _task_values(task: Task) -> dict[str, Any]:
    data = task.model_dump(mode="python")
    data["stage"] = task.stage.value
    data["status"] = task.status.value
    data["risk_level"] = task.risk_level.value
    data["error"] = task.error.model_dump() if task.error else None
    return data


def _to_domain(
    record: WorkflowRecord, tasks: list[TaskRecord], approvals: list[ApprovalRecord]
) -> Workflow:
    return Workflow(
        id=record.id,
        request=record.request,
        status=record.status,
        status_reason=record.status_reason,
        stage=record.stage,
        remediation_planned=record.remediation_planned,
        triage=record.triage,
        diagnosis=record.diagnosis,
        verification=record.verification,
        summary=record.summary,
        retry_count=record.retry_count,
        idempotency_key=record.idempotency_key,
        request_hash=record.request_hash,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
        completed_at=record.completed_at,
        tasks=[_task_to_domain(t) for t in tasks],
        approvals=[_approval_to_domain(a) for a in approvals],
    )


def _task_to_domain(record: TaskRecord) -> Task:
    return Task(
        id=record.id,
        key=record.key,
        stage=record.stage,
        sequence=record.sequence,
        description=record.description,
        tool_name=record.tool_name,
        arguments=record.arguments,
        depends_on=record.depends_on,
        input_from=record.input_from,
        status=record.status,
        risk_level=record.risk_level,
        policy=record.policy,
        requires_approval=record.requires_approval,
        attempts=record.attempts,
        idempotency_key=record.idempotency_key,
        resolved_arguments=record.resolved_arguments,
        output=record.output,
        error=TaskError.model_validate(record.error) if record.error else None,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )


def _approval_to_domain(record: ApprovalRecord) -> Approval:
    return Approval(
        id=record.id,
        workflow_id=record.workflow_id,
        task_id=record.task_id,
        task_key=record.task_key,
        tool_name=record.tool_name,
        arguments=record.arguments,
        arguments_hash=record.arguments_hash,
        risk_level=record.risk_level,
        reasons=record.reasons,
        status=record.status,
        requested_at=record.requested_at,
        decided_by=record.decided_by,
        decided_at=record.decided_at,
        comment=record.comment,
    )
