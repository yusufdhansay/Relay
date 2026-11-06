"""Approval endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header

from app.api.dependencies import ApprovalsDep
from app.schemas.api import ApprovalOut, DecisionOut, DecisionRequest, ErrorResponse, WorkflowOut

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])


@router.get("/pending", response_model=list[ApprovalOut], summary="Approvals waiting for a human")
async def pending_approvals(approvals: ApprovalsDep) -> list[ApprovalOut]:
    return [ApprovalOut.of(a) for a in await approvals.pending()]


@router.post(
    "/{approval_id}/decision",
    response_model=DecisionOut,
    responses={
        403: {"model": ErrorResponse, "description": "Missing or invalid approver token"},
        404: {"model": ErrorResponse, "description": "Approval not found"},
        409: {"model": ErrorResponse, "description": "Already decided or workflow not waiting"},
    },
    summary="Approve or reject a pending action",
)
async def decide(
    approval_id: str,
    body: DecisionRequest,
    approvals: ApprovalsDep,
    approver_token: Annotated[
        str | None,
        Header(
            alias="X-Approver-Token",
            description="Secret token that identifies the human approver.",
        ),
    ] = None,
) -> DecisionOut:
    result = await approvals.decide(
        approval_id,
        body.decision,
        token=approver_token,
        comment=body.comment,
        resume=body.resume,
    )
    return DecisionOut(
        approval=ApprovalOut.of(result.approval), workflow=WorkflowOut.of(result.workflow)
    )
