"""Workflow endpoints. Handlers only translate HTTP to service calls."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status

from app.api.dependencies import WorkflowsDep
from app.core.enums import WorkflowStatus
from app.schemas.api import (
    AgentRunOut,
    CancelRequest,
    ErrorResponse,
    EventList,
    EventOut,
    TaskOut,
    WorkflowCreate,
    WorkflowListItem,
    WorkflowOut,
    WorkflowPage,
)

router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])

ERRORS = {
    404: {"model": ErrorResponse, "description": "Workflow not found"},
    409: {"model": ErrorResponse, "description": "Not allowed in the current state"},
}
IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description="Repeat-safe submission key. Same key and body returns the same workflow.",
    ),
]


@router.post(
    "",
    response_model=WorkflowOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"model": WorkflowOut, "description": "Duplicate submission"},
        409: ERRORS[409],
    },
    summary="Submit a request and run it until it completes or needs approval",
)
async def submit_workflow(
    body: WorkflowCreate,
    workflows: WorkflowsDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> WorkflowOut:
    submission = await workflows.submit(body.request, idempotency_key=idempotency_key)
    if not submission.created:
        response.status_code = status.HTTP_200_OK
    return WorkflowOut.of(submission.workflow)


@router.get("", response_model=WorkflowPage, summary="List workflows")
async def list_workflows(
    workflows: WorkflowsDep,
    status_filter: Annotated[WorkflowStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> WorkflowPage:
    items, total = await workflows.list(status=status_filter, limit=limit, offset=offset)
    return WorkflowPage(
        items=[WorkflowListItem.of(w) for w in items], total=total, limit=limit, offset=offset
    )


@router.get("/{workflow_id}", response_model=WorkflowOut, responses={404: ERRORS[404]})
async def get_workflow(workflow_id: str, workflows: WorkflowsDep) -> WorkflowOut:
    return WorkflowOut.of(await workflows.get(workflow_id))


@router.get(
    "/{workflow_id}/events",
    response_model=EventList,
    responses={404: ERRORS[404]},
    summary="Chronological audit history",
)
async def get_events(workflow_id: str, workflows: WorkflowsDep) -> EventList:
    events = await workflows.events(workflow_id)
    return EventList(workflow_id=workflow_id, events=[EventOut.of(e) for e in events])


@router.get("/{workflow_id}/tasks", response_model=list[TaskOut], responses={404: ERRORS[404]})
async def get_tasks(workflow_id: str, workflows: WorkflowsDep) -> list[TaskOut]:
    return [TaskOut.of(t) for t in await workflows.tasks(workflow_id)]


@router.get(
    "/{workflow_id}/agent-runs",
    response_model=list[AgentRunOut],
    responses={404: ERRORS[404]},
    summary="Agent invocations with LLM latency and token usage",
)
async def get_agent_runs(workflow_id: str, workflows: WorkflowsDep) -> list[AgentRunOut]:
    runs = await workflows.agent_runs(workflow_id)
    return [AgentRunOut.model_validate(r.model_dump()) for r in runs]


@router.post(
    "/{workflow_id}/cancel",
    response_model=WorkflowOut,
    responses={404: ERRORS[404], 409: ERRORS[409]},
)
async def cancel_workflow(
    workflow_id: str, workflows: WorkflowsDep, body: CancelRequest | None = None
) -> WorkflowOut:
    reason = body.reason if body else None
    return WorkflowOut.of(await workflows.cancel(workflow_id, reason=reason))


@router.post(
    "/{workflow_id}/resume",
    response_model=WorkflowOut,
    responses={404: ERRORS[404], 409: ERRORS[409]},
    summary="Continue an approved or interrupted workflow",
)
async def resume_workflow(workflow_id: str, workflows: WorkflowsDep) -> WorkflowOut:
    return WorkflowOut.of(await workflows.resume(workflow_id))
