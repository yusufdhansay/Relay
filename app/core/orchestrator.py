"""Orchestrator: the deterministic coordinator of the agents.

The orchestrator owns every state transition. Agents return typed data; the orchestrator
validates it, applies it to the workflow aggregate, and persists after every step. Control
decisions (approvals, blocks, retries, final status) are made here in code, never by an LLM.

Lifecycle::

    RECEIVED -> TRIAGING -> PLANNING (investigation) -> EXECUTING
      -> [PLANNING (remediation) -> EXECUTING -> AWAITING_APPROVAL -> EXECUTING]
      -> VERIFYING -> COMPLETED | FAILED   (or CANCELLED at any point)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agents import (
    AgentInvocationError,
    ExecutionAgent,
    PlanningAgent,
    PolicyAgent,
    TriageAgent,
    VerificationAgent,
)
from app.agents.context import find_instruction_like
from app.core.config import Settings
from app.core.enums import (
    CHANGE_OUTCOMES,
    ApprovalStatus,
    EventType,
    PlanStage,
    PolicyVerdict,
    TaskStatus,
    VerificationRecommendation,
    WorkflowStatus,
)
from app.core.exceptions import (
    ConcurrencyConflictError,
    RelayError,
    WorkflowNotResumableError,
)
from app.core.locks import WorkflowLocks
from app.core.logging import get_logger
from app.core.workflow import AgentRun, Task, TaskError, Workflow
from app.database import Database
from app.repositories import WorkflowRepository
from app.schemas.agents import TriageResult
from app.services.summary import build_summary
from app.tools.registry import ToolRegistry

logger = get_logger("orchestrator")

W = WorkflowStatus


@dataclass
class AgentSet:
    triage: TriageAgent
    planner: PlanningAgent
    policy: PolicyAgent
    executor: ExecutionAgent
    verifier: VerificationAgent


class Orchestrator:
    component = "orchestrator"

    def __init__(
        self,
        *,
        database: Database,
        agents: AgentSet,
        registry: ToolRegistry,
        settings: Settings,
        locks: WorkflowLocks,
    ) -> None:
        self.database = database
        self.agents = agents
        self.registry = registry
        self.settings = settings
        self.locks = locks

    # ------------------------------------------------------------------ public API
    async def run(self, workflow_id: str) -> Workflow:
        """Drive the workflow until it is terminal or waiting for a human."""
        async with self.locks.hold(workflow_id):
            return await self._run_locked(workflow_id)

    async def resume(self, workflow_id: str) -> Workflow:
        async with self.locks.hold(workflow_id):
            workflow = await self._load(workflow_id)
            if workflow.status.is_terminal:
                raise WorkflowNotResumableError(
                    f"Workflow is {workflow.status.value} and cannot be resumed"
                )
            if workflow.status == W.AWAITING_APPROVAL and workflow.pending_approvals():
                raise WorkflowNotResumableError(
                    "Workflow is still waiting for an approval decision"
                )
            workflow.record(EventType.WORKFLOW_RESUMED, self.component, status=workflow.status)
            self._recover_interrupted_tasks(workflow)
            await self._save(workflow, [])
            return await self._run_locked(workflow_id)

    # ------------------------------------------------------------------ main loop
    async def _run_locked(self, workflow_id: str) -> Workflow:
        workflow = await self._load(workflow_id)
        for _ in range(self.settings.max_steps_per_run):
            if self._should_stop(workflow):
                break
            runs: list[AgentRun] = []
            try:
                runs = await self._step(workflow)
            except AgentInvocationError as exc:
                runs = exc.runs
                self._record_runs(workflow, runs)
                self._fail(workflow, f"{exc.agent} could not complete: {exc.message}")
            except RelayError as exc:
                self._fail(workflow, f"{exc.code}: {exc.message}")
            except Exception:
                logger.exception(
                    "orchestrator.unexpected_error", extra={"fields": {"id": workflow.id}}
                )
                self._fail(workflow, "Internal error while processing the workflow")
            if workflow.status.is_terminal and workflow.summary is None:
                workflow.summary = build_summary(workflow, self.registry)
            try:
                await self._save(workflow, runs)
            except ConcurrencyConflictError:
                logger.warning("orchestrator.conflict", extra={"fields": {"id": workflow.id}})
                return await self._load(workflow_id)
        else:
            self._fail(workflow, "Step budget exhausted; the workflow was stopped")
            workflow.summary = build_summary(workflow, self.registry)
            await self._save(workflow, [])
        return workflow

    @staticmethod
    def _should_stop(workflow: Workflow) -> bool:
        if workflow.status.is_terminal:
            return True
        return workflow.status == W.AWAITING_APPROVAL and bool(workflow.pending_approvals())

    async def _step(self, workflow: Workflow) -> list[AgentRun]:
        match workflow.status:
            case W.RECEIVED:
                workflow.transition_to(W.TRIAGING)
                return []
            case W.TRIAGING:
                return await self._triage(workflow)
            case W.PLANNING:
                return await self._plan(workflow)
            case W.EXECUTING:
                return await self._execute_next(workflow)
            case W.AWAITING_APPROVAL:
                self._apply_approval_decision(workflow)
                return []
            case W.VERIFYING:
                return await self._verify(workflow)
        raise WorkflowNotResumableError(f"No step for status {workflow.status.value}")

    # ------------------------------------------------------------------ triage
    async def _triage(self, workflow: Workflow) -> list[AgentRun]:
        agent = self.agents.triage
        workflow.record(EventType.AGENT_INVOKED, agent.name, operation="triage")
        result = await agent.run(workflow.id, workflow.request)
        self._record_runs(workflow, result.runs)
        triage = result.output
        workflow.triage = triage.model_dump(mode="json")
        workflow.record(
            EventType.REQUEST_CLASSIFIED,
            agent.name,
            category=triage.category,
            urgency=triage.urgency,
            services=triage.services,
            requested_outcomes=triage.requested_outcomes,
            read_only=triage.constraints.read_only,
            missing_information=triage.missing_information,
        )
        if not triage.can_proceed:
            reason = "; ".join(triage.missing_information) or "request is not supported"
            self._fail(workflow, f"Request cannot be handled: {reason}")
        else:
            workflow.transition_to(W.PLANNING, reason="triage complete")
        return result.runs

    # ------------------------------------------------------------------ planning
    async def _plan(self, workflow: Workflow) -> list[AgentRun]:
        triage = self._triage_result(workflow)
        planner = self.agents.planner
        stage = workflow.stage
        operation = "plan_investigation" if stage == PlanStage.INVESTIGATION else "plan_remediation"
        workflow.record(EventType.AGENT_INVOKED, planner.name, operation=operation)
        if stage == PlanStage.INVESTIGATION:
            result = await planner.plan_investigation(workflow, triage)
        else:
            result = await planner.plan_remediation(workflow, triage)
            workflow.remediation_planned = True
        runs = list(result.runs)
        self._record_runs(workflow, result.runs)
        plan = result.output
        if plan.diagnosis is not None:
            workflow.diagnosis = plan.diagnosis.model_dump(mode="json")
            workflow.record(
                EventType.DIAGNOSIS_RECORDED,
                planner.name,
                affected_service=plan.diagnosis.affected_service,
                root_cause=plan.diagnosis.root_cause,
                confidence=plan.diagnosis.confidence,
                fault_found=plan.diagnosis.fault_found,
            )
        workflow.record(
            EventType.PLAN_GENERATED,
            planner.name,
            stage=stage,
            objective=plan.objective,
            rationale=plan.rationale,
            tasks=[
                {
                    "task": t.key,
                    "tool": t.tool_name,
                    "arguments": t.arguments,
                    "depends_on": t.depends_on,
                    "input_from": t.input_from,
                }
                for t in plan.tasks
            ],
        )

        policy = self.agents.policy
        workflow.record(EventType.AGENT_INVOKED, policy.name, operation="policy_review")
        decisions = await policy.evaluate(
            workflow, plan.tasks, read_only=triage.constraints.read_only, stage=stage
        )
        runs.extend(decisions.runs)
        self._record_runs(workflow, decisions.runs)
        workflow.add_tasks(plan.tasks)
        for task, decision in zip(plan.tasks, decisions.output, strict=True):
            task.policy = decision.model_dump(mode="json")
            task.risk_level = decision.risk_level
            task.requires_approval = decision.verdict == PolicyVerdict.REQUIRE_APPROVAL
            if decision.verdict == PolicyVerdict.BLOCK:
                workflow.block_task(task, decision.reasons, component=policy.name)
        workflow.record(
            EventType.POLICY_EVALUATED,
            policy.name,
            stage=stage,
            decisions=[
                {
                    "task": d.task_key,
                    "tool": d.tool,
                    "verdict": d.verdict,
                    "risk_level": d.risk_level,
                    "rules": d.rules,
                }
                for d in decisions.output
            ],
        )
        workflow.transition_to(W.EXECUTING, reason=f"{stage.value.lower()} plan accepted")
        return runs

    # ------------------------------------------------------------------ execution
    async def _execute_next(self, workflow: Workflow) -> list[AgentRun]:
        stage = workflow.stage
        task = workflow.next_task(stage)
        if task is None:
            self._skip_unreachable(workflow, stage)
            if stage == PlanStage.INVESTIGATION and self._needs_remediation(workflow):
                workflow.stage = PlanStage.REMEDIATION
                workflow.transition_to(W.PLANNING, reason="investigation complete")
            else:
                workflow.transition_to(W.VERIFYING, reason="all tasks settled")
            return []

        if task.requires_approval:
            approval = workflow.approval_for_task(task)
            if approval is None or approval.status != ApprovalStatus.APPROVED:
                reasons = (task.policy or {}).get("reasons", ["approval required"])
                workflow.request_approval(task, reasons=reasons)
                workflow.transition_to(
                    W.AWAITING_APPROVAL,
                    reason=f"{task.key} ({task.tool_name}) needs human approval",
                )
                return []
        return await self._run_task(workflow, task)

    async def _run_task(self, workflow: Workflow, task: Task) -> list[AgentRun]:
        executor = self.agents.executor
        runs: list[AgentRun] = []
        workflow.record(
            EventType.TOOL_SELECTED,
            executor.name,
            task=task.key,
            tool=task.tool_name,
            stage=task.stage,
            needs_argument_resolution=bool(task.input_from),
        )
        if task.input_from:
            workflow.record(EventType.AGENT_INVOKED, executor.name, operation="resolve_arguments")
        try:
            resolved = await executor.resolve_arguments(workflow, task)
        except AgentInvocationError as exc:
            self._record_runs(workflow, exc.runs)
            workflow.start_task(task)
            workflow.fail_task(
                task, TaskError(code="argument_resolution_failed", message=exc.message[:500])
            )
            workflow.record(
                EventType.TOOL_FAILED, executor.name, task=task.key, error=exc.message[:500]
            )
            return exc.runs
        runs.extend(resolved.runs)
        self._record_runs(workflow, resolved.runs)
        task.resolved_arguments = resolved.output

        # Persist RUNNING before touching the tool, so a crash leaves a visible in-flight task.
        workflow.start_task(task)
        await self._save(workflow, runs)
        runs = []

        outcome = await executor.execute(workflow, task, resolved.output)
        for attempt in outcome.attempts:
            event = (
                EventType.RETRY_ATTEMPTED
                if attempt.delay_seconds is not None
                else EventType.TOOL_FAILED
            )
            workflow.record(
                event,
                executor.name,
                task=task.key,
                tool=task.tool_name,
                attempt=attempt.attempt,
                error_code=attempt.error_code,
                error=attempt.error,
                retry_in_seconds=attempt.delay_seconds,
            )
        if outcome.success:
            workflow.complete_task(task, outcome.output or {})
            workflow.record(
                EventType.TOOL_EXECUTED,
                executor.name,
                task=task.key,
                tool=task.tool_name,
                arguments=outcome.arguments,
                attempts=len(outcome.attempts) + 1,
                duration_ms=round(outcome.duration_ms, 2),
                permit=outcome.permit_basis,
                replayed=bool((outcome.output or {}).get("replayed")),
            )
            suspicious = find_instruction_like(outcome.output)
            if suspicious:
                workflow.record(
                    EventType.UNTRUSTED_CONTENT_FLAGGED,
                    self.component,
                    task=task.key,
                    tool=task.tool_name,
                    samples=suspicious,
                    note="Tool output contains instruction-like text; it is treated as data only",
                )
        else:
            if outcome.error_code == "execution_not_authorized":
                workflow.record(
                    EventType.ACTION_BLOCKED,
                    "execution_guard",
                    task=task.key,
                    tool=task.tool_name,
                    reasons=[outcome.error],
                )
            workflow.fail_task(
                task,
                TaskError(
                    code=outcome.error_code or "tool_failed",
                    message=outcome.error or "tool failed",
                    retryable=outcome.retryable,
                ),
            )
        return runs

    def _skip_unreachable(self, workflow: Workflow, stage: PlanStage) -> None:
        for task in workflow.stage_tasks(stage):
            if task.status == TaskStatus.PENDING:
                workflow.skip_task(task, reason="dependencies can no longer be met")

    def _needs_remediation(self, workflow: Workflow) -> bool:
        triage = self._triage_result(workflow)
        wants_change = bool(set(triage.requested_outcomes) & CHANGE_OUTCOMES)
        if not wants_change or workflow.remediation_planned:
            return False
        if triage.constraints.read_only:
            workflow.record(
                EventType.PLAN_REJECTED,
                self.component,
                stage=PlanStage.REMEDIATION,
                reason="remediation skipped because the request is read-only",
            )
            return False
        has_evidence = any(
            t.status == TaskStatus.SUCCEEDED for t in workflow.stage_tasks(PlanStage.INVESTIGATION)
        )
        return has_evidence

    # ------------------------------------------------------------------ approvals
    def _apply_approval_decision(self, workflow: Workflow) -> None:
        waiting = [t for t in workflow.tasks if t.status == TaskStatus.AWAITING_APPROVAL]
        if not waiting:
            workflow.transition_to(W.EXECUTING, reason="no task is waiting for approval")
            return
        task = waiting[0]
        approval = workflow.approval_for_task(task)
        if approval is None or approval.status == ApprovalStatus.PENDING:
            return
        if approval.status == ApprovalStatus.APPROVED:
            workflow.transition_to(
                W.EXECUTING, reason=f"{task.key} approved by {approval.decided_by}"
            )
            return
        workflow.reject_task(task, approval)
        workflow.cancel(
            f"{task.key} ({task.tool_name}) was rejected by {approval.decided_by}"
            + (f": {approval.comment}" if approval.comment else ""),
            component=self.component,
        )

    # ------------------------------------------------------------------ verification
    async def _verify(self, workflow: Workflow) -> list[AgentRun]:
        verifier = self.agents.verifier
        workflow.record(EventType.AGENT_INVOKED, verifier.name, operation="verify")
        result = await verifier.verify(workflow, self._triage_result(workflow))
        self._record_runs(workflow, result.runs)
        verification = result.output
        workflow.verification = verification.model_dump(mode="json")
        workflow.record(
            EventType.VERIFICATION_COMPLETED,
            verifier.name,
            objective_achieved=verification.objective_achieved,
            recommendation=verification.recommendation,
            failed_checks=[c.name for c in verification.checks if not c.passed],
            probes=verification.probes,
        )
        match verification.recommendation:
            case VerificationRecommendation.COMPLETE:
                workflow.transition_to(W.COMPLETED, reason="verification passed")
            case VerificationRecommendation.RETRY if (
                workflow.retry_count < self.settings.max_workflow_retries
            ):
                self._retry(workflow, verification.retry_task_keys)
            case VerificationRecommendation.RETRY:
                self._fail(workflow, f"Retry budget exhausted. {verification.summary}")
            case _:
                self._fail(workflow, verification.summary)
        return result.runs

    def _retry(self, workflow: Workflow, keys: list[str]) -> None:
        workflow.retry_count += 1
        workflow.record(
            EventType.WORKFLOW_RETRY, self.component, attempt=workflow.retry_count, tasks=keys
        )
        reset: set[str] = set()
        for key in keys:
            self._reset_chain(workflow, workflow.get_task(key), reset)
        stages = [workflow.get_task(k).stage for k in reset]
        workflow.stage = (
            PlanStage.INVESTIGATION if PlanStage.INVESTIGATION in stages else workflow.stage
        )
        workflow.verification = None
        workflow.transition_to(W.EXECUTING, reason=f"retry {workflow.retry_count}")

    def _reset_chain(self, workflow: Workflow, task: Task, reset: set[str]) -> None:
        if task.key in reset or task.status not in {TaskStatus.FAILED, TaskStatus.SKIPPED}:
            return
        workflow.reset_task_for_retry(task, reason="workflow retry")
        reset.add(task.key)
        for other in workflow.tasks:
            if task.key in other.depends_on:
                self._reset_chain(workflow, other, reset)

    # ------------------------------------------------------------------ recovery
    def _recover_interrupted_tasks(self, workflow: Workflow) -> None:
        """Handle tasks left RUNNING by a crash.

        Idempotent tools are reset to PENDING; they keep their idempotency key, so a repeat
        replays the recorded result instead of acting twice. Anything else is failed with an
        explicit "outcome unknown" error for a human to check.
        """
        for task in workflow.tasks:
            if task.status != TaskStatus.RUNNING:
                continue
            tool = self.registry.get(task.tool_name)
            if tool.idempotent:
                workflow.requeue_interrupted_task(task)
                action = "reset_for_replay"
            else:
                workflow.fail_task(
                    task,
                    TaskError(
                        code="interrupted",
                        message="Execution was interrupted; the outcome is unknown",
                    ),
                )
                action = "failed_outcome_unknown"
            workflow.record(
                EventType.WORKFLOW_RECOVERED,
                self.component,
                task=task.key,
                tool=task.tool_name,
                action=action,
                idempotency_key=task.idempotency_key,
            )

    # ------------------------------------------------------------------ helpers
    def _triage_result(self, workflow: Workflow) -> TriageResult:
        if workflow.triage is None:
            raise WorkflowNotResumableError("Workflow has no triage result")
        return TriageResult.model_validate(workflow.triage)

    def _fail(self, workflow: Workflow, reason: str) -> None:
        if not workflow.status.is_terminal:
            workflow.fail(reason[:1000], component=self.component)

    @staticmethod
    def _record_runs(workflow: Workflow, runs: list[AgentRun]) -> None:
        for run in runs:
            details: dict[str, Any] = {
                "operation": run.operation,
                "provider": run.provider,
                "model": run.model,
                "llm_calls": run.llm_calls,
                "latency_ms": run.latency_ms,
                "prompt_tokens": run.prompt_tokens,
                "completion_tokens": run.completion_tokens,
            }
            if run.success:
                workflow.record(EventType.AGENT_COMPLETED, run.agent, **details)
            else:
                workflow.record(EventType.AGENT_FAILED, run.agent, error=run.error, **details)

    async def _load(self, workflow_id: str) -> Workflow:
        async with self.database.session() as session:
            return await WorkflowRepository(session).get(workflow_id)

    async def _save(self, workflow: Workflow, runs: list[AgentRun]) -> None:
        async with self.database.session() as session:
            repo = WorkflowRepository(session)
            await repo.save(workflow)
            if runs:
                await repo.add_agent_runs(runs)
