"""Simulated IT operations tools.

Each tool is a thin adapter over :class:`SimulatedEnvironment`. Replacing the environment
with real clients (monitoring API, log store, deployment system) would not change the
registry, policy, or orchestration code.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated, Any, ClassVar, Literal

from pydantic import Field, field_validator

from app.core.enums import RiskLevel
from app.tools.base import Tool, ToolContext, ToolInput, ToolOutput
from app.tools.operations.environment import (
    CONFIG_LIMITS,
    REFERENCE_TIME,
    SEVERITY_ORDER,
    SimulatedEnvironment,
)
from app.tools.registry import ToolRegistry

ServiceName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{1,48}$")]
Severity = Literal["DEBUG", "INFO", "WARN", "ERROR"]
ServiceStatus = Literal["HEALTHY", "DEGRADED", "DOWN"]


class _EnvTool[InT: ToolInput, OutT: ToolOutput](Tool[InT, OutT]):
    def __init__(self, env: SimulatedEnvironment) -> None:
        self.env = env


# ---------------------------------------------------------------------- get_service_health
class HealthInput(ToolInput):
    service: ServiceName | None = Field(
        default=None, description="Service to check. Omit to check every service."
    )


class ServiceHealth(ToolOutput):
    service: str
    status: ServiceStatus
    latency_p50_ms: float
    latency_p95_ms: float
    error_rate_percent: float
    uptime_seconds: int
    version: str
    tier: int
    checked_at: datetime


class HealthOutput(ToolOutput):
    services: list[ServiceHealth]


class GetServiceHealth(_EnvTool[HealthInput, HealthOutput]):
    name: ClassVar[str] = "get_service_health"
    description: ClassVar[str] = (
        "Current health of one service (or all services): status, latency, error rate, uptime."
    )
    input_model = HealthInput
    output_model = HealthOutput
    risk_level = RiskLevel.LOW
    mutating = False
    idempotent = True

    async def run(self, args: HealthInput, ctx: ToolContext) -> HealthOutput:
        await self.env.before_call(self.name)
        names = [args.service] if args.service else sorted(self.env.services)
        results = []
        for name in names:
            service = self.env.service(name)
            metrics = self.env.current_metrics(service)
            results.append(
                ServiceHealth(
                    service=name,
                    status=self.env.status(service),
                    latency_p50_ms=metrics.latency_p50_ms,
                    latency_p95_ms=metrics.latency_p95_ms,
                    error_rate_percent=metrics.error_rate_percent,
                    uptime_seconds=service.uptime_seconds,
                    version=service.version,
                    tier=service.tier,
                    checked_at=REFERENCE_TIME,
                )
            )
        return HealthOutput(services=results)


# ---------------------------------------------------------------------- get_service_metrics
class MetricsInput(ToolInput):
    service: ServiceName
    window_minutes: int = Field(default=60, ge=5, le=1440)


class MetricPoint(ToolOutput):
    timestamp: datetime
    latency_p50_ms: float
    latency_p95_ms: float
    error_rate_percent: float
    cpu_percent: float
    memory_percent: float
    db_pool_active: int
    requests_per_minute: int


class MetricsOutput(ToolOutput):
    service: str
    window_minutes: int
    interval_minutes: int
    db_pool_max: int
    current: MetricPoint
    baseline: MetricPoint
    series: list[MetricPoint]
    anomalies: list[str]


def _point(timestamp: datetime, m: Any) -> MetricPoint:
    return MetricPoint(
        timestamp=timestamp,
        latency_p50_ms=m.latency_p50_ms,
        latency_p95_ms=m.latency_p95_ms,
        error_rate_percent=m.error_rate_percent,
        cpu_percent=m.cpu_percent,
        memory_percent=m.memory_percent,
        db_pool_active=m.db_pool_active,
        requests_per_minute=m.requests_per_minute,
    )


class GetServiceMetrics(_EnvTool[MetricsInput, MetricsOutput]):
    name: ClassVar[str] = "get_service_metrics"
    description: ClassVar[str] = (
        "Current, baseline, and historical metrics (latency, errors, CPU, memory, DB pool) "
        "with detected anomalies."
    )
    input_model = MetricsInput
    output_model = MetricsOutput
    risk_level = RiskLevel.LOW
    mutating = False
    idempotent = True

    async def run(self, args: MetricsInput, ctx: ToolContext) -> MetricsOutput:
        await self.env.before_call(self.name)
        service = self.env.service(args.service)
        current = self.env.current_metrics(service)
        baseline = service.baseline
        interval, series = self.env.metric_series(service, args.window_minutes)
        pool_max = int(service.config.get("db_pool_max_size", 0))
        anomalies = []
        for label, now, base in (
            ("latency_p95_ms", current.latency_p95_ms, baseline.latency_p95_ms),
            ("error_rate_percent", current.error_rate_percent, baseline.error_rate_percent),
        ):
            if base and now / base >= 2:
                anomalies.append(f"{label} is {now / base:.1f}x baseline ({now:g} vs {base:g})")
        if current.memory_percent >= 90:
            anomalies.append(f"memory_percent is {current.memory_percent:g}% (>= 90%)")
        if pool_max and current.db_pool_active >= pool_max:
            anomalies.append(f"db pool saturated ({current.db_pool_active}/{pool_max} active)")
        return MetricsOutput(
            service=args.service,
            window_minutes=args.window_minutes,
            interval_minutes=interval,
            db_pool_max=pool_max,
            current=_point(REFERENCE_TIME, current),
            baseline=_point(REFERENCE_TIME, baseline),
            series=[_point(ts, values) for ts, values in series],
            anomalies=anomalies,
        )


# ---------------------------------------------------------------------- search_service_logs
class LogsInput(ToolInput):
    service: ServiceName
    min_severity: Severity = "WARN"
    window_minutes: int = Field(default=60, ge=5, le=1440)
    query: str | None = Field(default=None, max_length=100)
    limit: int = Field(default=50, ge=1, le=100)


class LogEntry(ToolOutput):
    timestamp: datetime
    service: str
    severity: Severity
    message: str


class LogsOutput(ToolOutput):
    service: str
    total_matched: int
    truncated: bool
    entries: list[LogEntry]


class SearchServiceLogs(_EnvTool[LogsInput, LogsOutput]):
    name: ClassVar[str] = "search_service_logs"
    description: ClassVar[str] = (
        "Search a service's logs by minimum severity, time window, and optional text query."
    )
    input_model = LogsInput
    output_model = LogsOutput
    risk_level = RiskLevel.LOW
    mutating = False
    idempotent = True

    async def run(self, args: LogsInput, ctx: ToolContext) -> LogsOutput:
        await self.env.before_call(self.name)
        service = self.env.service(args.service)
        threshold = SEVERITY_ORDER[args.min_severity]
        start = REFERENCE_TIME.timestamp() - args.window_minutes * 60
        query = args.query.lower() if args.query else None
        matched = [
            LogEntry(timestamp=ts, service=args.service, severity=sev, message=msg)  # type: ignore[arg-type]
            for ts, sev, msg in self.env.logs(service)
            if SEVERITY_ORDER[sev] >= threshold
            and ts.timestamp() >= start
            and (query is None or query in msg.lower())
        ]
        return LogsOutput(
            service=args.service,
            total_matched=len(matched),
            truncated=len(matched) > args.limit,
            entries=matched[: args.limit],
        )


# ---------------------------------------------------------------------- restart_service
class RestartInput(ToolInput):
    service: ServiceName
    reason: str = Field(min_length=3, max_length=300)


class RestartOutput(ToolOutput):
    service: str
    restarted: bool
    restart_number: int
    previous_uptime_seconds: int
    status_after: ServiceStatus
    replayed: bool = False


class RestartService(_EnvTool[RestartInput, RestartOutput]):
    name: ClassVar[str] = "restart_service"
    description: ClassVar[str] = (
        "Restart a simulated service. Clears transient faults such as leaked connections. "
        "Requires human approval."
    )
    input_model = RestartInput
    output_model = RestartOutput
    risk_level = RiskLevel.MEDIUM
    mutating = True
    idempotent = True  # repeated calls with the same idempotency key replay the result

    async def run(self, args: RestartInput, ctx: ToolContext) -> RestartOutput:
        previous = self.env.replay(ctx.idempotency_key)
        if previous is not None:
            return RestartOutput(**previous, replayed=True)
        failure = await self.env.before_call(self.name)
        result = self.env.restart(args.service, args.reason, ctx.idempotency_key)
        await self.env.after_effect(failure)
        return RestartOutput(**result)


# ---------------------------------------------------------------------- update_service_config
ConfigValue = int | bool | str


class ConfigInput(ToolInput):
    service: ServiceName
    changes: dict[str, ConfigValue] = Field(min_length=1, max_length=5)
    reason: str = Field(min_length=3, max_length=300)

    @field_validator("changes")
    @classmethod
    def _check_changes(cls, changes: dict[str, ConfigValue]) -> dict[str, ConfigValue]:
        for key, value in changes.items():
            if key not in CONFIG_LIMITS:
                allowed = ", ".join(sorted(CONFIG_LIMITS))
                raise ValueError(f"'{key}' is not a configurable key (allowed: {allowed})")
            kind, low, high = CONFIG_LIMITS[key]
            if kind is bool:
                if not isinstance(value, bool):
                    raise ValueError(f"'{key}' must be a boolean")
            elif kind is int:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(f"'{key}' must be an integer")
                if not low <= value <= high:
                    raise ValueError(f"'{key}' must be between {low} and {high}")
            elif not isinstance(value, str) or value not in low:
                raise ValueError(f"'{key}' must be one of {sorted(low)}")
        return changes


class ConfigChange(ToolOutput):
    old: ConfigValue | None
    new: ConfigValue


class ConfigOutput(ToolOutput):
    service: str
    applied: dict[str, ConfigChange]
    status_after: ServiceStatus
    replayed: bool = False


class UpdateServiceConfig(_EnvTool[ConfigInput, ConfigOutput]):
    name: ClassVar[str] = "update_service_config"
    description: ClassVar[str] = (
        "Change up to five allowlisted configuration keys of a simulated service. "
        "Requires human approval."
    )
    input_model = ConfigInput
    output_model = ConfigOutput
    risk_level = RiskLevel.HIGH
    mutating = True
    idempotent = True

    async def run(self, args: ConfigInput, ctx: ToolContext) -> ConfigOutput:
        previous = self.env.replay(ctx.idempotency_key)
        if previous is not None:
            return ConfigOutput(**previous, replayed=True)
        failure = await self.env.before_call(self.name)
        result = self.env.update_config(args.service, dict(args.changes), ctx.idempotency_key)
        await self.env.after_effect(failure)
        return ConfigOutput(**result)


# ---------------------------------------------------------------------- generate_incident_report
class EvidenceItem(ToolInput):
    source: str = Field(min_length=1, max_length=60)
    detail: str = Field(min_length=1, max_length=400)


class ReportInput(ToolInput):
    service: ServiceName
    title: str = Field(min_length=5, max_length=120)
    severity: Literal["SEV1", "SEV2", "SEV3", "SEV4"]
    summary: str = Field(min_length=10, max_length=2000)
    findings: list[Annotated[str, Field(max_length=400)]] = Field(min_length=1, max_length=20)
    evidence: list[EvidenceItem] = Field(default_factory=list, max_length=30)
    actions_taken: list[Annotated[str, Field(max_length=300)]] = Field(
        default_factory=list, max_length=20
    )
    recommendations: list[Annotated[str, Field(max_length=300)]] = Field(
        default_factory=list, max_length=20
    )


class ReportOutput(ToolOutput):
    report_id: str
    service: str
    title: str
    severity: str
    created_at: datetime
    markdown: str
    replayed: bool = False


class GenerateIncidentReport(_EnvTool[ReportInput, ReportOutput]):
    name: ClassVar[str] = "generate_incident_report"
    description: ClassVar[str] = (
        "Write a structured incident report (summary, findings, evidence, actions, "
        "recommendations). Does not change any service."
    )
    input_model = ReportInput
    output_model = ReportOutput
    risk_level = RiskLevel.LOW
    mutating = False
    idempotent = True

    async def run(self, args: ReportInput, ctx: ToolContext) -> ReportOutput:
        previous = self.env.replay(ctx.idempotency_key)
        if previous is not None:
            return ReportOutput(**previous, replayed=True)
        await self.env.before_call(self.name)
        digest = hashlib.sha256(f"{ctx.workflow_id}:{ctx.task_key}".encode()).hexdigest()[:10]
        report = {
            "report_id": f"INC-{digest.upper()}",
            "service": args.service,
            "title": args.title,
            "severity": args.severity,
            "created_at": REFERENCE_TIME,
            "markdown": _render_report(args),
        }
        return ReportOutput(**self.env.store_report(report, ctx.idempotency_key))


def _render_report(args: ReportInput) -> str:
    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items) if items else "- None"

    evidence = (
        "\n".join(f"- **{e.source}**: {e.detail}" for e in args.evidence)
        if args.evidence
        else "- None"
    )
    return (
        f"# {args.title}\n\n"
        f"**Service:** {args.service}  \n**Severity:** {args.severity}\n\n"
        f"## Summary\n{args.summary}\n\n"
        f"## Findings\n{bullets(args.findings)}\n\n"
        f"## Evidence\n{evidence}\n\n"
        f"## Actions taken\n{bullets(args.actions_taken)}\n\n"
        f"## Recommendations\n{bullets(args.recommendations)}\n"
    )


# ---------------------------------------------------------------------- purge_service_logs
class PurgeInput(ToolInput):
    service: ServiceName
    older_than_hours: int = Field(ge=1, le=720)


class PurgeOutput(ToolOutput):
    service: str
    deleted_entries: int
    older_than_hours: int


class PurgeServiceLogs(_EnvTool[PurgeInput, PurgeOutput]):
    name: ClassVar[str] = "purge_service_logs"
    description: ClassVar[str] = (
        "Delete operational log data for a service. Critical risk: always blocked by policy."
    )
    input_model = PurgeInput
    output_model = PurgeOutput
    risk_level = RiskLevel.CRITICAL
    mutating = True
    idempotent = False

    async def run(self, args: PurgeInput, ctx: ToolContext) -> PurgeOutput:
        await self.env.before_call(self.name)
        return PurgeOutput(
            **self.env.purge_logs(args.service, args.older_than_hours, ctx.idempotency_key)
        )


OPERATION_TOOLS: tuple[type[_EnvTool[Any, Any]], ...] = (
    GetServiceHealth,
    GetServiceMetrics,
    SearchServiceLogs,
    RestartService,
    UpdateServiceConfig,
    GenerateIncidentReport,
    PurgeServiceLogs,
)


def build_operations_registry(
    env: SimulatedEnvironment, *, default_timeout_seconds: float = 10.0
) -> ToolRegistry:
    registry = ToolRegistry(default_timeout_seconds=default_timeout_seconds)
    for tool_cls in OPERATION_TOOLS:
        registry.register(tool_cls(env))
    return registry
