"""Composition root: build every component from settings in one place."""

from __future__ import annotations

from dataclasses import dataclass

from app.agents import (
    AgentInfo,
    ExecutionAgent,
    PlanningAgent,
    PolicyAgent,
    TriageAgent,
    VerificationAgent,
)
from app.agents.executor import Sleeper
from app.core.config import Settings
from app.core.guard import ExecutionGuard
from app.core.locks import WorkflowLocks
from app.core.orchestrator import AgentSet, Orchestrator
from app.core.policy import PolicyEngine
from app.database import Database
from app.llm.base import LLMProvider
from app.llm.factory import build_provider
from app.services.approvals import ApprovalService
from app.services.workflows import WorkflowService
from app.tools.operations import SimulatedEnvironment, build_operations_registry
from app.tools.registry import ToolRegistry

ORCHESTRATOR_INFO = AgentInfo(
    name="orchestrator",
    description="Deterministic coordinator that owns all workflow state transitions.",
    responsibilities=[
        "Hand work between agents in a fixed, validated order",
        "Apply policy decisions, pause for approvals, and enforce rejections",
        "Persist progress and audit events after every step",
        "Bound loops, retries, and recover interrupted workflows",
    ],
    uses_llm=False,
    output_schema=None,
)


@dataclass
class Container:
    settings: Settings
    database: Database
    environment: SimulatedEnvironment
    registry: ToolRegistry
    policy_engine: PolicyEngine
    guard: ExecutionGuard
    llm: LLMProvider
    agents: AgentSet
    locks: WorkflowLocks
    orchestrator: Orchestrator
    workflows: WorkflowService
    approvals: ApprovalService

    @classmethod
    def build(
        cls,
        settings: Settings,
        *,
        environment: SimulatedEnvironment | None = None,
        llm: LLMProvider | None = None,
        sleep: Sleeper | None = None,
    ) -> Container:
        environment = environment or SimulatedEnvironment()
        llm = llm or build_provider(settings)
        database = Database(settings.database_url)
        registry = build_operations_registry(
            environment, default_timeout_seconds=settings.tool_timeout_seconds
        )
        engine = PolicyEngine(registry, settings.critical_service_names())
        guard = ExecutionGuard(engine)
        repair = settings.agent_output_repair_attempts
        executor_kwargs = {"sleep": sleep} if sleep else {}
        agents = AgentSet(
            triage=TriageAgent(
                llm, known_services=sorted(environment.services), repair_attempts=repair
            ),
            planner=PlanningAgent(
                llm, registry=registry, max_tasks=settings.max_plan_tasks, repair_attempts=repair
            ),
            policy=PolicyAgent(llm, engine=engine, repair_attempts=repair),
            executor=ExecutionAgent(
                llm,
                registry=registry,
                guard=guard,
                max_attempts=settings.tool_max_attempts,
                base_delay_seconds=settings.retry_base_delay_seconds,
                max_delay_seconds=settings.retry_max_delay_seconds,
                timeout_seconds=settings.tool_timeout_seconds,
                repair_attempts=repair,
                **executor_kwargs,
            ),
            verifier=VerificationAgent(llm, registry=registry, guard=guard, repair_attempts=repair),
        )
        locks = WorkflowLocks()
        orchestrator = Orchestrator(
            database=database, agents=agents, registry=registry, settings=settings, locks=locks
        )
        workflows = WorkflowService(
            database=database, orchestrator=orchestrator, locks=locks, registry=registry
        )
        return cls(
            settings=settings,
            database=database,
            environment=environment,
            registry=registry,
            policy_engine=engine,
            guard=guard,
            llm=llm,
            agents=agents,
            locks=locks,
            orchestrator=orchestrator,
            workflows=workflows,
            approvals=ApprovalService(
                settings=settings, database=database, orchestrator=orchestrator, locks=locks
            ),
        )

    async def startup(self) -> None:
        if self.settings.auto_create_schema:
            await self.database.create_schema()
        if self.settings.recover_on_startup:
            await self.workflows.recover_interrupted()

    async def shutdown(self) -> None:
        await self.llm.aclose()
        await self.database.dispose()

    def agent_infos(self) -> list[AgentInfo]:
        a = self.agents
        return [
            ORCHESTRATOR_INFO,
            *(agent.info() for agent in (a.triage, a.planner, a.policy, a.executor, a.verifier)),
        ]
