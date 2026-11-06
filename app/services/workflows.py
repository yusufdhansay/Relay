"""Workflow application service: the entry point used by the API, CLI, and evaluations."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError

from app.core.enums import EventType, WorkflowStatus
from app.core.exceptions import DuplicateRequestConflictError
from app.core.locks import WorkflowLocks
from app.core.orchestrator import Orchestrator
from app.core.workflow import AgentRun, Task, Workflow, WorkflowEvent
from app.database import Database
from app.repositories import WorkflowRepository
from app.services.summary import build_summary
from app.tools.registry import ToolRegistry


@dataclass
class Submission:
    workflow: Workflow
    created: bool


def request_hash(request: str) -> str:
    return hashlib.sha256(request.strip().encode()).hexdigest()


class WorkflowService:
    def __init__(
        self,
        *,
        database: Database,
        orchestrator: Orchestrator,
        locks: WorkflowLocks,
        registry: ToolRegistry,
    ) -> None:
        self.database = database
        self.orchestrator = orchestrator
        self.locks = locks
        self.registry = registry

    async def submit(self, request: str, *, idempotency_key: str | None = None) -> Submission:
        """Create a workflow and run it until it finishes or needs a human.

        With an idempotency key, a repeated submission returns the original workflow instead
        of starting a second one. Reusing a key for a different request is an error.
        """
        request = request.strip()
        digest = request_hash(request)
        if idempotency_key:
            existing = await self._find_duplicate(idempotency_key, digest)
            if existing is not None:
                return Submission(existing, created=False)

        workflow = Workflow.create(request, idempotency_key=idempotency_key, request_hash=digest)
        try:
            async with self.database.session() as session:
                await WorkflowRepository(session).save(workflow)
        except IntegrityError:
            # Another request with the same key won the race.
            existing = await self._find_duplicate(idempotency_key or "", digest)
            if existing is None:
                raise
            return Submission(existing, created=False)

        return Submission(await self.orchestrator.run(workflow.id), created=True)

    async def _find_duplicate(self, key: str, digest: str) -> Workflow | None:
        async with self.database.session() as session:
            repo = WorkflowRepository(session)
            existing = await repo.find_by_idempotency_key(key)
            if existing is None:
                return None
            if existing.request_hash != digest:
                raise DuplicateRequestConflictError(
                    "This Idempotency-Key was already used for a different request"
                )
            existing.record(EventType.DUPLICATE_REQUEST, "api", idempotency_key=key)
            await repo.save(existing)
            return existing

    async def get(self, workflow_id: str) -> Workflow:
        async with self.database.session() as session:
            return await WorkflowRepository(session).get(workflow_id)

    async def list(
        self, *, status: WorkflowStatus | None, limit: int, offset: int
    ) -> tuple[list[Workflow], int]:
        async with self.database.session() as session:
            return await WorkflowRepository(session).list(status=status, limit=limit, offset=offset)

    async def events(self, workflow_id: str) -> list[WorkflowEvent]:
        async with self.database.session() as session:
            repo = WorkflowRepository(session)
            if not await repo.exists(workflow_id):
                await repo.get(workflow_id)  # raises WorkflowNotFoundError
            return await repo.list_events(workflow_id)

    async def tasks(self, workflow_id: str) -> list[Task]:
        return (await self.get(workflow_id)).tasks

    async def agent_runs(self, workflow_id: str) -> list[AgentRun]:
        async with self.database.session() as session:
            repo = WorkflowRepository(session)
            await repo.get(workflow_id)
            return await repo.list_agent_runs(workflow_id)

    async def cancel(self, workflow_id: str, *, reason: str | None = None) -> Workflow:
        async with self.locks.hold(workflow_id), self.database.session() as session:
            repo = WorkflowRepository(session)
            workflow = await repo.get(workflow_id)
            workflow.cancel(reason or "Cancelled by user", component="api")
            workflow.summary = build_summary(workflow, self.registry)
            await repo.save(workflow)
            return workflow

    async def resume(self, workflow_id: str) -> Workflow:
        return await self.orchestrator.resume(workflow_id)

    async def recover_interrupted(self) -> list[str]:
        """Resume every workflow left mid-run by a previous process."""
        async with self.database.session() as session:
            ids = await WorkflowRepository(session).list_unfinished_ids()
        for workflow_id in ids:
            await self.orchestrator.resume(workflow_id)
        return ids
