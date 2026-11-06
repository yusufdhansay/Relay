"""Context management: shrink tool outputs before they are sent to a model.

Tool outputs can be large (metric series, many log lines). Agents only need the parts
that carry signal, and every byte sent to a model costs tokens and latency.
"""

from __future__ import annotations

from typing import Any

from app.core.workflow import Task

MAX_LOG_ENTRIES = 20
MAX_SERIES_POINTS = 4
MAX_TEXT = 600


def compact_output(tool_name: str, output: dict[str, Any] | None) -> dict[str, Any] | None:
    if output is None:
        return None
    data = dict(output)
    if tool_name == "get_service_metrics" and isinstance(data.get("series"), list):
        data["series"] = data["series"][-MAX_SERIES_POINTS:]
    if tool_name == "search_service_logs" and isinstance(data.get("entries"), list):
        data["entries"] = [
            {**entry, "message": str(entry.get("message", ""))[:MAX_TEXT]}
            for entry in data["entries"][:MAX_LOG_ENTRIES]
        ]
    if tool_name == "generate_incident_report":
        data.pop("markdown", None)
    return data


def task_view(task: Task, *, include_output: bool = True) -> dict[str, Any]:
    """A compact, JSON-safe description of a task for agent payloads."""
    view: dict[str, Any] = {
        "task_id": task.key,
        "stage": task.stage.value,
        "tool": task.tool_name,
        "description": task.description,
        "arguments": task.resolved_arguments or task.arguments,
        "status": task.status.value,
    }
    if task.error:
        view["error"] = {"code": task.error.code, "message": task.error.message[:MAX_TEXT]}
    if include_output:
        view["output"] = compact_output(task.tool_name, task.output)
    return view
