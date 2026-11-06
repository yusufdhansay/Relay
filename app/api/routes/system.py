"""System information endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from app import __version__
from app.agents import AgentInfo
from app.api.dependencies import ContainerDep
from app.tools.base import ToolSpec

router = APIRouter(tags=["system"])


@router.get("/health")
async def health(container: ContainerDep) -> dict[str, Any]:
    database = "ok"
    try:
        async with container.database.session() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        database = "unavailable"
    return {
        "status": "ok" if database == "ok" else "degraded",
        "version": __version__,
        "environment": container.settings.environment,
        "llm_provider": container.llm.name,
        "database": database,
    }


@router.get("/api/v1/tools", response_model=list[ToolSpec], summary="Registered tools and risk")
async def list_tools(container: ContainerDep) -> list[ToolSpec]:
    return container.registry.specs()


@router.get("/api/v1/agents", response_model=list[AgentInfo], summary="Agents and their roles")
async def list_agents(container: ContainerDep) -> list[AgentInfo]:
    return container.agent_infos()


@router.post(
    "/api/v1/simulation/reset",
    summary="Reset the simulated environment to its initial faults (not in production)",
)
async def reset_simulation(container: ContainerDep) -> dict[str, Any]:
    if container.settings.environment == "production":
        raise HTTPException(status_code=404, detail="Not Found")
    container.environment.reset()
    return {
        "status": "reset",
        "services": {
            name: service.fault.value for name, service in container.environment.services.items()
        },
    }
