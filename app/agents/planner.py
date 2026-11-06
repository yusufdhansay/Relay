"""Planning Agent: turn a triaged request into an ordered, validated task plan.

Planning happens in two stages:

1. Investigation: read-only diagnostic tasks.
2. Remediation: after the evidence is in, propose a diagnosis and (optionally) changes.

Every plan is checked in code before it is accepted: tools must be registered and allowed
for the stage, dependencies must point to earlier tasks, and arguments must match the tool
schema. A plan that fails the checks is sent back to the model once with the problems.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agents.base import Agent, AgentResult
from app.agents.context import task_view
from app.core.enums import PlanStage
from app.core.exceptions import ToolArgumentError, ToolNotFoundError
from app.core.workflow import Task, Workflow
from app.schemas.agents import Diagnosis, ExecutionPlan, PlannedTask, RemediationPlan, TriageResult
from app.tools.registry import ToolRegistry


@dataclass
class PlanningOutput:
    stage: PlanStage
    tasks: list[Task]
    objective: str
    rationale: str
    diagnosis: Diagnosis | None = None


class PlanningAgent(Agent):
    name = "planning_agent"
    description = "Builds ordered task plans from registered tools, in two stages."
    responsibilities = (
        "Break the request into ordered tasks with explicit dependencies",
        "Select only registered tools that are allowed for the current stage",
        "Fill tool arguments that match each tool's input schema",
        "After investigation, state a diagnosis with evidence and propose remediation",
        "Respect the requester's constraints",
    )
    output_schema_name = "ExecutionPlan | RemediationPlan"
    system_prompt = (
        "You are the Planning Agent. Build the smallest plan that achieves the objective "
        "using ONLY the tools listed in INPUT.tools. Never invent tools or arguments.\n"
        "- Each task has a short id, a description, a tool, and arguments that match the "
        "tool's input_schema.\n"
        "- depends_on may only reference earlier task ids.\n"
        "- When an argument can only be known from an earlier task's output (for example "
        "which service is degraded), leave it out and list that task in input_from (and "
        "depends_on).\n"
        "- Investigation plans gather evidence (health, metrics, logs) and write a report "
        "only if the user asked for one.\n"
        "- Remediation plans first give a diagnosis citing evidence by task_id, then propose "
        "the single most appropriate change. Use an empty task list if no change is needed "
        "or allowed. Changes must have complete arguments (no input_from).\n"
        "- You do not decide approvals. A separate policy step does that."
    )

    def __init__(
        self, llm, *, registry: ToolRegistry, max_tasks: int = 10, repair_attempts: int = 1
    ) -> None:
        super().__init__(llm, repair_attempts=repair_attempts)
        self.registry = registry
        self.max_tasks = max_tasks

    # ------------------------------------------------------------------ investigation
    async def plan_investigation(
        self, workflow: Workflow, triage: TriageResult
    ) -> AgentResult[PlanningOutput]:
        allowed = [s for s in self.registry.specs() if not s.mutating]
        allowed_names = {s.name for s in allowed}
        payload = {
            "request": workflow.request,
            "triage": triage.model_dump(mode="json"),
            "tools": [_tool_view(s) for s in allowed],
            "max_tasks": self.max_tasks,
        }
        plan, run = await self._generate(
            workflow_id=workflow.id,
            operation="plan_investigation",
            payload=payload,
            output_model=ExecutionPlan,
            semantic_validator=lambda p: self.validate_tasks(
                p.tasks, allowed_names, require_tasks=True
            ),
        )
        tasks = self._to_tasks(plan.tasks, PlanStage.INVESTIGATION, start=1, prefix="I")
        output = PlanningOutput(PlanStage.INVESTIGATION, tasks, plan.objective, plan.rationale)
        return AgentResult(output, [run])

    # ------------------------------------------------------------------ remediation
    async def plan_remediation(
        self, workflow: Workflow, triage: TriageResult
    ) -> AgentResult[PlanningOutput]:
        specs = self.registry.specs()
        allowed_names = {s.name for s in specs}
        evidence_ids = {t.key for t in workflow.tasks}
        payload = {
            "request": workflow.request,
            "triage": triage.model_dump(mode="json"),
            "findings": [task_view(t) for t in workflow.stage_tasks(PlanStage.INVESTIGATION)],
            "tools": [_tool_view(s) for s in specs],
            "max_tasks": self.max_tasks,
        }

        def validate(plan: RemediationPlan) -> list[str]:
            problems = self.validate_tasks(plan.tasks, allowed_names, require_tasks=False)
            for ref in plan.diagnosis.evidence:
                if ref.task_id not in evidence_ids:
                    problems.append(f"Evidence cites unknown task '{ref.task_id}'")
            for task in plan.tasks:
                if task.input_from and self._is_mutating(task.tool):
                    problems.append(
                        f"Task {task.id}: changing actions need complete arguments (no input_from)"
                    )
            return problems

        plan, run = await self._generate(
            workflow_id=workflow.id,
            operation="plan_remediation",
            payload=payload,
            output_model=RemediationPlan,
            semantic_validator=validate,
        )
        start = len(workflow.tasks) + 1
        tasks = self._to_tasks(plan.tasks, PlanStage.REMEDIATION, start=start, prefix="R")
        output = PlanningOutput(
            PlanStage.REMEDIATION,
            tasks,
            objective=plan.diagnosis.root_cause,
            rationale=plan.rationale,
            diagnosis=plan.diagnosis,
        )
        return AgentResult(output, [run])

    # ------------------------------------------------------------------ validation
    def validate_tasks(
        self, tasks: list[PlannedTask], allowed: set[str], *, require_tasks: bool
    ) -> list[str]:
        problems: list[str] = []
        if require_tasks and not tasks:
            problems.append("The plan must contain at least one task")
        if len(tasks) > self.max_tasks:
            problems.append(f"The plan has {len(tasks)} tasks; the maximum is {self.max_tasks}")
        seen: set[str] = set()
        for task in tasks:
            if task.id in seen:
                problems.append(f"Duplicate task id '{task.id}'")
            if task.tool not in allowed:
                problems.append(
                    f"Task {task.id}: tool '{task.tool}' is not available "
                    f"(choose from {sorted(allowed)})"
                )
            for dep in task.depends_on:
                if dep not in seen:
                    problems.append(f"Task {task.id}: depends_on '{dep}' is not an earlier task")
            for source in task.input_from:
                if source not in task.depends_on:
                    problems.append(f"Task {task.id}: input_from '{source}' must be in depends_on")
            if task.tool in allowed:
                problems.extend(self._argument_problems(task))
            seen.add(task.id)
        return problems

    def _argument_problems(self, task: PlannedTask) -> list[str]:
        try:
            tool = self.registry.get(task.tool)
        except ToolNotFoundError:
            return [f"Task {task.id}: unknown tool '{task.tool}'"]
        if task.input_from:
            unknown = set(task.arguments) - set(tool.input_model.model_fields)
            return [f"Task {task.id}: unknown arguments {sorted(unknown)}"] if unknown else []
        try:
            self.registry.validate_arguments(task.tool, task.arguments)
        except ToolArgumentError as exc:
            return [f"Task {task.id}: {exc.message}"]
        return []

    def _is_mutating(self, tool_name: str) -> bool:
        return self.registry.has(tool_name) and self.registry.get(tool_name).mutating

    def _to_tasks(
        self, planned: list[PlannedTask], stage: PlanStage, *, start: int, prefix: str
    ) -> list[Task]:
        keys = {p.id: f"{prefix}{i}" for i, p in enumerate(planned, start=1)}
        tasks = []
        for offset, p in enumerate(planned):
            arguments: dict[str, Any] = (
                dict(p.arguments)
                if p.input_from
                else self.registry.normalise_arguments(p.tool, p.arguments)
            )
            tasks.append(
                Task(
                    key=keys[p.id],
                    stage=stage,
                    sequence=start + offset,
                    description=p.description,
                    tool_name=p.tool,
                    arguments=arguments,
                    depends_on=[keys[d] for d in p.depends_on],
                    input_from=[keys[d] for d in p.input_from],
                    risk_level=self.registry.get(p.tool).risk_level,
                )
            )
        return tasks


def _tool_view(spec: Any) -> dict[str, Any]:
    return {
        "name": spec.name,
        "description": spec.description,
        "risk_level": spec.risk_level.value,
        "changes_infrastructure": spec.mutating,
        "input_schema": spec.input_schema,
    }
