"""Rule-based responders for the deterministic mock provider (added with the agents)."""

from __future__ import annotations

from app.llm.mock_provider import Responder

DEFAULT_RESPONDERS: dict[str, Responder] = {}
