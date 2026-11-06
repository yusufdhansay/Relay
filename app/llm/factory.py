"""Build the configured LLM provider."""

from __future__ import annotations

from app.core.config import Settings
from app.core.exceptions import ConfigurationError
from app.llm.base import LLMProvider
from app.llm.mock_provider import MockProvider
from app.llm.openai_provider import OpenAIProvider


def build_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "mock":
        return MockProvider()
    if settings.llm_provider == "openai":
        if settings.openai_api_key is None or not settings.openai_api_key.get_secret_value():
            raise ConfigurationError(
                "RELAY_LLM_PROVIDER=openai requires RELAY_OPENAI_API_KEY. "
                "Set the key or use RELAY_LLM_PROVIDER=mock."
            )
        return OpenAIProvider(
            api_key=settings.openai_api_key.get_secret_value(),
            model=settings.llm_model,
            base_url=settings.openai_base_url,
            temperature=settings.llm_temperature,
            timeout_seconds=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
    raise ConfigurationError(f"Unknown LLM provider: {settings.llm_provider}")
