"""Run evaluation cases against a fresh Relay instance each and compute metrics.

Two groups of metrics are reported separately:

* System guarantees: properties enforced by code (approvals, policy blocks, read-only
  adherence, no duplicate side effects, failure handling). These should hold for any
  model.
* Model quality: properties that depend on the LLM (classification, tool selection,
  first-attempt output validity, verification accuracy). In mock mode these measure the
  rule-based mock responders, not a language model.
"""

from __future__ import annotations

import statistics
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from app.core.config import Settings
from app.core.enums import ApprovalDecision, ApprovalStatus, TaskStatus, WorkflowStatus
from app.core.exceptions import LLMProviderError
from app.core.workflow import fingerprint
from app.llm.base import LLMProvider, LLMRequest, LLMResponse
from app.llm.factory import build_provider
from app.services.container import Container
from app.tools.operations import SimulatedEnvironment
from evals.cases import CASES, EvalCase

APPROVER_TOKEN = "eval-approver-token"  # noqa: S105  (local, in-process evaluation only)


class FaultInjectingProvider(LLMProvider):
    """Wraps a provider and fails chosen operations a fixed number of times."""

    def __init__(self, inner: LLMProvider, failures: dict[str, int]) -> None:
        self.inner = inner
        self.failures = dict(failures)
        self.name = inner.name
        self.model = inner.model

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if self.failures.get(request.operation, 0) > 0:
            self.failures[request.operation] -= 1
            raise LLMProviderError("Injected provider outage", retryable=False)
        return await self.inner.complete(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


@dataclass
class CaseResult:
    id: str
    tags: list[str]
    expected_status: str
    status: str
    status_reason: str | None
    category: str | None
    expected_category: str
    read_only: bool | None
    expected_read_only: bool
    planned_tools: list[str]
    required_tools: list[str]
    allowed_tools: list[str]
    approvals_requested: list[str]
    expected_approvals: list[str]
    objective_achieved: bool | None
    expected_objective: bool | None
    violations: list[str]
    mutations: int
    duplicate_side_effects: int
    task_statuses: dict[str, str]
    agent_runs: int
    first_attempt_valid_runs: int
    resolve_runs: int
    resolve_first_attempt_valid: int
    llm_latency_ms: float
    prompt_tokens: int | None
    completion_tokens: int | None
    wall_ms: float
    error: str | None = None

    @property
    def status_ok(self) -> bool:
        return self.status == self.expected_status


@dataclass
class EvalReport:
    provider: str
    model: str
    cases: list[CaseResult] = field(default_factory=list)
    system: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)
    performance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "system": self.system,
            "quality": self.quality,
            "performance": self.performance,
            "cases": [asdict(c) | {"status_ok": c.status_ok} for c in self.cases],
        }


async def run_case(case: EvalCase, base: Settings, workdir: Path) -> CaseResult:
    env = SimulatedEnvironment()
    if case.setup:
        case.setup(env)
    settings = base.model_copy(
        update={
            "database_url": f"sqlite+aiosqlite:///{workdir / f'{case.id}.db'}",
            "approver_tokens": SecretStr(f"eval-approver:{APPROVER_TOKEN}"),
            **case.settings,
        }
    )
    llm: LLMProvider = build_provider(settings)
    if case.llm_failures:
        llm = FaultInjectingProvider(llm, case.llm_failures)
    container = Container.build(settings, environment=env, llm=llm)
    await container.startup()
    started = time.perf_counter()
    error = None
    try:
        workflow = (await container.workflows.submit(case.request)).workflow
        decisions = list(case.decisions)
        while workflow.status == WorkflowStatus.AWAITING_APPROVAL:
            approval = workflow.pending_approvals()[0]
            action = decisions.pop(0) if decisions else "cancel"
            if action == "cancel":
                workflow = await container.workflows.cancel(workflow.id, reason="evaluation")
                break
            result = await container.approvals.decide(
                approval.id, ApprovalDecision(action), token=APPROVER_TOKEN
            )
            workflow = result.workflow
    except Exception as exc:  # recorded as a case error, never hidden
        error = f"{type(exc).__name__}: {exc}"
        workflow = None
    wall_ms = (time.perf_counter() - started) * 1000

    if workflow is None:
        await container.shutdown()
        return _errored(case, error or "unknown error", wall_ms)

    workflow = await container.workflows.get(workflow.id)
    runs = await container.workflows.agent_runs(workflow.id)
    await container.shutdown()

    triage = workflow.triage or {}
    verification = workflow.verification or {}
    violations = _violations(case, workflow, env, container)
    keys = Counter(m["idempotency_key"] for m in env.mutations)
    prompt = [r.prompt_tokens for r in runs if r.prompt_tokens is not None]
    completion = [r.completion_tokens for r in runs if r.completion_tokens is not None]
    resolve = [r for r in runs if r.operation == "resolve_arguments"]
    return CaseResult(
        id=case.id,
        tags=list(case.tags),
        expected_status=case.expected_status,
        status=workflow.status.value,
        status_reason=workflow.status_reason,
        category=triage.get("category"),
        expected_category=case.expected_category,
        read_only=(triage.get("constraints") or {}).get("read_only"),
        expected_read_only=case.expected_read_only,
        planned_tools=sorted({t.tool_name for t in workflow.tasks}),
        required_tools=sorted(case.required_tools),
        allowed_tools=sorted(case.allowed_tools),
        approvals_requested=[a.tool_name for a in workflow.approvals],
        expected_approvals=list(case.expected_approvals),
        objective_achieved=verification.get("objective_achieved"),
        expected_objective=case.expected_objective,
        violations=violations,
        mutations=len(env.mutations),
        duplicate_side_effects=sum(count - 1 for count in keys.values() if count > 1),
        task_statuses={t.key: t.status.value for t in workflow.tasks},
        agent_runs=len(runs),
        first_attempt_valid_runs=sum(1 for r in runs if r.success and r.llm_calls == 1),
        resolve_runs=len(resolve),
        resolve_first_attempt_valid=sum(1 for r in resolve if r.success and r.llm_calls == 1),
        llm_latency_ms=round(sum(r.latency_ms for r in runs), 3),
        prompt_tokens=sum(prompt) if prompt else None,
        completion_tokens=sum(completion) if completion else None,
        wall_ms=round(wall_ms, 2),
    )


def _violations(case: EvalCase, workflow, env: SimulatedEnvironment, container) -> list[str]:
    """Check real side effects in the environment against approvals and policy."""
    problems = []
    tasks_by_key = {t.idempotency_key: t for t in workflow.tasks}
    for mutation in env.mutations:
        task = tasks_by_key.get(mutation["idempotency_key"])
        if task is None:
            problems.append(f"side effect {mutation['tool']} has no task")
            continue
        approval = workflow.approval_for_task(task)
        args = task.resolved_arguments or task.arguments
        if (
            approval is None
            or approval.status != ApprovalStatus.APPROVED
            or approval.arguments_hash != fingerprint(task.tool_name, args)
        ):
            problems.append(f"{task.key} {task.tool_name} ran without a matching approval")
        if task.status in {TaskStatus.BLOCKED, TaskStatus.REJECTED, TaskStatus.CANCELLED}:
            problems.append(f"{task.key} {task.tool_name} ran although {task.status.value}")
    if case.expected_read_only and env.mutations:
        problems.append(f"read-only request caused {len(env.mutations)} change(s)")
    for tool in case.forbidden_tools:
        if env.calls.get(tool):
            problems.append(f"forbidden tool {tool} was invoked")
    mutating = {s.name for s in container.registry.specs() if s.mutating}
    for task in workflow.tasks:
        if (
            task.tool_name in mutating
            and task.status == TaskStatus.SUCCEEDED
            and workflow.approval_for_task(task) is None
        ):
            problems.append(f"{task.key} succeeded without any approval record")
    return problems


def _errored(case: EvalCase, error: str, wall_ms: float) -> CaseResult:
    return CaseResult(
        id=case.id,
        tags=list(case.tags),
        expected_status=case.expected_status,
        status="ERROR",
        status_reason=error,
        category=None,
        expected_category=case.expected_category,
        read_only=None,
        expected_read_only=case.expected_read_only,
        planned_tools=[],
        required_tools=sorted(case.required_tools),
        allowed_tools=sorted(case.allowed_tools),
        approvals_requested=[],
        expected_approvals=list(case.expected_approvals),
        objective_achieved=None,
        expected_objective=case.expected_objective,
        violations=[],
        mutations=0,
        duplicate_side_effects=0,
        task_statuses={},
        agent_runs=0,
        first_attempt_valid_runs=0,
        resolve_runs=0,
        resolve_first_attempt_valid=0,
        llm_latency_ms=0.0,
        prompt_tokens=None,
        completion_tokens=None,
        wall_ms=round(wall_ms, 2),
        error=error,
    )


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "value": round(numerator / denominator, 4) if denominator else None,
        "numerator": numerator,
        "denominator": denominator,
    }


def summarise(report: EvalReport) -> None:
    cases = report.cases
    by_tag = lambda tag: [c for c in cases if tag in c.tags]  # noqa: E731

    rejection = by_tag("rejection")
    failure = by_tag("failure")
    read_only_cases = [c for c in cases if c.expected_read_only]
    report.system = {
        "workflow_status_accuracy": _rate(sum(c.status_ok for c in cases), len(cases)),
        "policy_compliance_rate": _rate(sum(not c.violations for c in cases), len(cases)),
        "approval_enforcement_rate": _rate(
            sum(c.mutations for c in cases if not c.violations),
            sum(c.mutations for c in cases),
        ),
        "approval_detection_accuracy": _rate(
            sum(c.approvals_requested == c.expected_approvals for c in cases), len(cases)
        ),
        "read_only_adherence": _rate(
            sum(c.mutations == 0 for c in read_only_cases), len(read_only_cases)
        ),
        "rejection_handling": _rate(
            sum(c.status == "CANCELLED" and c.mutations == 0 for c in rejection), len(rejection)
        ),
        "failure_handling": _rate(sum(c.status_ok for c in failure), len(failure)),
        "duplicate_side_effects": sum(c.duplicate_side_effects for c in cases),
        "case_errors": sum(c.error is not None for c in cases),
    }

    planned_ok = planned_total = required_hit = required_total = 0
    for c in cases:
        acceptable = set(c.required_tools) | set(c.allowed_tools)
        planned = set(c.planned_tools)
        planned_ok += len(planned & acceptable)
        planned_total += len(planned)
        required_hit += len(planned & set(c.required_tools))
        required_total += len(c.required_tools)
    verified = [
        c for c in cases if c.expected_objective is not None and c.objective_achieved is not None
    ]
    expected_complete = [c for c in cases if c.expected_status == "COMPLETED"]
    tasks = [s for c in cases for s in c.task_statuses.values()]
    runnable = [s for s in tasks if s not in {"BLOCKED", "REJECTED", "CANCELLED"}]
    report.quality = {
        "classification_accuracy": _rate(
            sum(c.category == c.expected_category for c in cases), len(cases)
        ),
        "read_only_detection_accuracy": _rate(
            sum(
                bool(c.read_only) == c.expected_read_only for c in cases if c.read_only is not None
            ),
            sum(c.read_only is not None for c in cases),
        ),
        "tool_selection_precision": _rate(planned_ok, planned_total),
        "tool_selection_recall": _rate(required_hit, required_total),
        "first_attempt_output_validity": _rate(
            sum(c.first_attempt_valid_runs for c in cases), sum(c.agent_runs for c in cases)
        ),
        "tool_argument_validity": _rate(
            sum(c.resolve_first_attempt_valid for c in cases), sum(c.resolve_runs for c in cases)
        ),
        "verification_accuracy": _rate(
            sum(c.objective_achieved == c.expected_objective for c in verified), len(verified)
        ),
        "workflow_completion_rate": _rate(
            sum(c.status == "COMPLETED" for c in expected_complete), len(expected_complete)
        ),
        "task_success_rate": _rate(sum(s == "SUCCEEDED" for s in runnable), len(runnable)),
    }

    walls = sorted(c.wall_ms for c in cases)
    prompt = [c.prompt_tokens for c in cases if c.prompt_tokens is not None]
    completion = [c.completion_tokens for c in cases if c.completion_tokens is not None]
    report.performance = {
        "workflow_latency_ms": {
            "mean": round(statistics.mean(walls), 2),
            "p50": round(statistics.median(walls), 2),
            "p95": round(walls[min(len(walls) - 1, int(0.95 * len(walls)))], 2),
            "max": round(walls[-1], 2),
        },
        "llm_latency_ms_total": round(sum(c.llm_latency_ms for c in cases), 2),
        "agent_runs_total": sum(c.agent_runs for c in cases),
        "prompt_tokens_total": sum(prompt) if prompt else None,
        "completion_tokens_total": sum(completion) if completion else None,
    }


async def run_suite(
    settings: Settings | None = None, cases: tuple[EvalCase, ...] = CASES
) -> EvalReport:
    base = settings or Settings(
        environment="test",
        log_level="ERROR",
        retry_base_delay_seconds=0.01,
        retry_max_delay_seconds=0.05,
    )
    provider = build_provider(base)
    report = EvalReport(provider=provider.name, model=provider.model)
    await provider.aclose()
    with tempfile.TemporaryDirectory(prefix="relay-eval-") as tmp:
        for case in cases:
            report.cases.append(await run_case(case, base, Path(tmp)))
    summarise(report)
    return report
