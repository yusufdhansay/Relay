"""Full-system fixtures: a container wired to a temporary SQLite database."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.enums import ApprovalDecision
from app.core.workflow import Workflow
from app.llm.base import LLMProvider
from app.repositories import WorkflowRepository
from app.services.container import Container
from app.tools.operations import SimulatedEnvironment

SCENARIO_1 = (
    "Investigate why the payment service is experiencing high latency and resolve it if possible."
)
SCENARIO_2 = (
    "Investigate the recent increase in payment service errors and prepare an incident report. "
    "Do not make any changes."
)
SCENARIO_3 = "Investigate the service and apply the recommended configuration change."

APPROVER_TOKENS = "alice:token-alice-123,bob:token-bob-456"


def test_settings(db_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "database_url": f"sqlite+aiosqlite:///{db_path}",
        "log_level": "WARNING",
        "approver_tokens": APPROVER_TOKENS,
        "retry_base_delay_seconds": 0.0,
        "retry_max_delay_seconds": 0.0,
        "tool_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return Settings(**values)


async def build_container(
    db_path: Path,
    *,
    env: SimulatedEnvironment | None = None,
    llm: LLMProvider | None = None,
    **overrides: Any,
) -> Container:
    container = Container.build(test_settings(db_path, **overrides), environment=env, llm=llm)
    await container.startup()
    return container


async def decide_directly(
    container: Container,
    workflow: Workflow,
    decision: ApprovalDecision,
    *,
    by: str = "alice",
    comment: str | None = None,
) -> Workflow:
    """Record a decision at the domain level, then resume (bypasses HTTP auth)."""
    approval = workflow.pending_approvals()[0]
    async with container.database.session() as session:
        repo = WorkflowRepository(session)
        loaded = await repo.get(workflow.id)
        loaded.decide_approval(approval.id, decision, decided_by=by, comment=comment)
        await repo.save(loaded)
    return await container.workflows.resume(workflow.id)


async def event_types(container: Container, workflow_id: str) -> list[str]:
    return [e.event_type.value for e in await container.workflows.events(workflow_id)]
