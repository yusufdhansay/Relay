"""Builders for agent-level tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agents import ExecutionAgent, PlanningAgent, PolicyAgent, TriageAgent, VerificationAgent
from app.core.enums import PlanStage, TaskStatus, WorkflowStatus
from app.core.guard import ExecutionGuard
from app.core.policy import PolicyEngine
from app.core.workflow import Task, Workflow, fingerprint
from app.llm.base import LLMProvider
from app.llm.mock_provider import MockProvider
from app.tools.base import ExecutionPermit, ToolContext
from app.tools.operations import SimulatedEnvironment, build_operations_registry
from app.tools.registry import ToolRegistry

CRITICAL_SERVICES = ("payment-service", "auth-service")


@dataclass
class Components:
    env: SimulatedEnvironment
    registry: ToolRegistry
    engine: PolicyEngine
    guard: ExecutionGuard
    llm: LLMProvider
    triage: TriageAgent
    planner: PlanningAgent
    policy: PolicyAgent
    executor: ExecutionAgent
    verifier: VerificationAgent
    sleeps: list[float]


def build_components(
    llm: LLMProvider | None = None, env: SimulatedEnvironment | None = None
) -> Components:
    env = env or SimulatedEnvironment()
    llm = llm or MockProvider()
    registry = build_operations_registry(env, default_timeout_seconds=1.0)
    engine = PolicyEngine(registry, CRITICAL_SERVICES)
    guard = ExecutionGuard(engine)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return Components(
        env=env,
        registry=registry,
        engine=engine,
        guard=guard,
        llm=llm,
        triage=TriageAgent(llm, known_services=sorted(env.services)),
        planner=PlanningAgent(llm, registry=registry, max_tasks=10),
        policy=PolicyAgent(llm, engine=engine),
        executor=ExecutionAgent(llm, registry=registry, guard=guard, sleep=fake_sleep),
        verifier=VerificationAgent(llm, registry=registry, guard=guard),
        sleeps=sleeps,
    )


def executing_workflow(request: str, triage: dict[str, Any]) -> Workflow:
    wf = Workflow.create(request)
    wf.triage = triage
    for status in (WorkflowStatus.TRIAGING, WorkflowStatus.PLANNING, WorkflowStatus.EXECUTING):
        wf.transition_to(status)
    return wf


async def run_task_directly(c: Components, wf: Workflow, task: Task) -> None:
    """Execute a read-only task outside the orchestrator (test setup only)."""
    args = c.registry.normalise_arguments(task.tool_name, task.arguments)
    permit = ExecutionPermit(task.tool_name, fingerprint(task.tool_name, args), "policy_allow")
    result = await c.registry.invoke(
        task.tool_name, args, ToolContext(wf.id, task.key, task.idempotency_key), permit
    )
    wf.start_task(task)
    task.resolved_arguments = args
    wf.complete_task(task, result.output)


def investigation_task(key: str, seq: int, tool: str, args: dict[str, Any]) -> Task:
    return Task(
        key=key,
        stage=PlanStage.INVESTIGATION,
        sequence=seq,
        description=f"{tool} {args}",
        tool_name=tool,
        arguments=args,
    )


def succeeded(wf: Workflow) -> list[str]:
    return [t.key for t in wf.tasks if t.status == TaskStatus.SUCCEEDED]
