"""Render an evaluation report as Markdown."""

from __future__ import annotations

import platform
from datetime import UTC, datetime
from typing import Any

from evals.runner import EvalReport


def _fmt(metric: dict[str, Any]) -> str:
    if metric["value"] is None:
        return "n/a (no cases)"
    return f"{metric['value'] * 100:.1f}% ({metric['numerator']}/{metric['denominator']})"


def render_markdown(report: EvalReport) -> str:
    lines = [
        "# Relay evaluation results",
        "",
        f"- Generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        f"- Provider: `{report.provider}`, model: `{report.model}`",
        f"- Cases: {len(report.cases)}",
        f"- Python {platform.python_version()} on {platform.system()} {platform.machine()}",
        "- Each case runs on a fresh SQLite database and a fresh simulated environment.",
        "",
        "## System guarantees (enforced by code)",
        "",
        "| Metric | Result |",
        "|---|---|",
    ]
    for name, metric in report.system.items():
        value = _fmt(metric) if isinstance(metric, dict) else str(metric)
        lines.append(f"| {name.replace('_', ' ')} | {value} |")
    lines += [
        "",
        "## Model quality (depends on the LLM provider)",
        "",
        "| Metric | Result |",
        "|---|---|",
    ]
    for name, metric in report.quality.items():
        lines.append(f"| {name.replace('_', ' ')} | {_fmt(metric)} |")
    perf = report.performance
    latency = perf["workflow_latency_ms"]
    tokens = (
        f"{perf['prompt_tokens_total']} prompt / {perf['completion_tokens_total']} completion"
        if perf["prompt_tokens_total"] is not None
        else "not reported by this provider"
    )
    lines += [
        "",
        "## Performance",
        "",
        "| Metric | Result |",
        "|---|---|",
        f"| workflow latency mean / p50 / p95 / max | {latency['mean']} / {latency['p50']} / "
        f"{latency['p95']} / {latency['max']} ms |",
        f"| total LLM latency | {perf['llm_latency_ms_total']} ms |",
        f"| agent runs | {perf['agent_runs_total']} |",
        f"| tokens | {tokens} |",
        "",
        "## Cases",
        "",
        "| Case | Expected | Actual | Category ok | Approvals | Violations | ms |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in report.cases:
        approvals = ", ".join(c.approvals_requested) or "-"
        lines.append(
            f"| {c.id} | {c.expected_status} | {c.status} | "
            f"{'yes' if c.category == c.expected_category else 'no'} | {approvals} | "
            f"{len(c.violations)} | {c.wall_ms} |"
        )
    failures = [c for c in report.cases if not c.status_ok or c.violations or c.error]
    if failures:
        lines += ["", "## Cases that did not meet expectations", ""]
        for c in failures:
            lines.append(
                f"- **{c.id}**: expected {c.expected_status}, got {c.status}. "
                f"Reason: {c.status_reason}. Violations: {c.violations or 'none'}"
            )
    return "\n".join(lines) + "\n"
