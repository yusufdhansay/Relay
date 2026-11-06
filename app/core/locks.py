"""In-process, per-workflow locks.

Only one operation may drive a workflow at a time. A second caller gets ``WorkflowBusyError``
immediately instead of waiting. Across processes, optimistic versioning in the repository
is the backstop.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.core.exceptions import WorkflowBusyError


class WorkflowLocks:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def hold(self, workflow_id: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault(workflow_id, asyncio.Lock())
        if lock.locked():
            raise WorkflowBusyError(f"Workflow {workflow_id} is already being processed")
        try:
            async with lock:
                yield
        finally:
            if not lock.locked():
                self._locks.pop(workflow_id, None)

    def is_locked(self, workflow_id: str) -> bool:
        lock = self._locks.get(workflow_id)
        return bool(lock and lock.locked())
