"""Application settings loaded from environment variables (prefix ``RELAY_``)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration for Relay.

    Secrets are typed as ``SecretStr`` so they never appear in ``repr`` output or logs.
    """

    model_config = SettingsConfigDict(
        env_prefix="RELAY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    app_name: str = "Relay"
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"

    # Persistence
    database_url: str = "sqlite+aiosqlite:///./data/relay.db"
    auto_create_schema: bool = True

    # LLM provider
    llm_provider: Literal["mock", "openai"] = "mock"
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    openai_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("RELAY_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )
    openai_base_url: str = "https://api.openai.com/v1"

    # Human approvers, format: "alice:token-a,bob:token-b"
    approver_tokens: SecretStr | None = None

    # Orchestration limits
    max_steps_per_run: int = Field(default=60, ge=1, le=500)
    max_plan_tasks: int = Field(default=10, ge=1, le=50)
    max_workflow_retries: int = Field(default=1, ge=0, le=5)
    agent_output_repair_attempts: int = Field(default=1, ge=0, le=3)

    # Tool execution
    tool_timeout_seconds: float = Field(default=10.0, gt=0)
    tool_max_attempts: int = Field(default=3, ge=1, le=10)
    retry_base_delay_seconds: float = Field(default=0.2, ge=0)
    retry_max_delay_seconds: float = Field(default=2.0, ge=0)

    # Recovery
    recover_on_startup: bool = False

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.upper()

    def approvers(self) -> dict[str, str]:
        """Return a mapping of approver token to approver name."""
        if self.approver_tokens is None:
            return {}
        mapping: dict[str, str] = {}
        for raw in self.approver_tokens.get_secret_value().split(","):
            entry = raw.strip()
            if not entry:
                continue
            name, sep, token = entry.partition(":")
            if not sep or not name.strip() or not token.strip():
                raise ValueError("RELAY_APPROVER_TOKENS entries must look like 'name:token'")
            mapping[token.strip()] = name.strip()
        return mapping


@lru_cache
def get_settings() -> Settings:
    return Settings()
