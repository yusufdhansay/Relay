"""FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.services.approvals import ApprovalService
from app.services.container import Container
from app.services.workflows import WorkflowService


def get_container(request: Request) -> Container:
    return request.app.state.container


def get_workflows(container: Annotated[Container, Depends(get_container)]) -> WorkflowService:
    return container.workflows


def get_approvals(container: Annotated[Container, Depends(get_container)]) -> ApprovalService:
    return container.approvals


ContainerDep = Annotated[Container, Depends(get_container)]
WorkflowsDep = Annotated[WorkflowService, Depends(get_workflows)]
ApprovalsDep = Annotated[ApprovalService, Depends(get_approvals)]
