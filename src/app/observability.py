"""Optional LangSmith tracing, off by default.

The LangSmith client reads process environment variables, but this app loads
`.env` through pydantic settings, which do not export anything. At startup
``configure_tracing`` reads the ``LANGSMITH_*`` settings and exports them so
LangGraph runs and Claude calls are traced exactly when they are enabled.
"""

import logging
import os

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


LOGGER = logging.getLogger(__name__)


class TracingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "kubesage"
    # Only for a non-default region or a self-hosted LangSmith.
    langsmith_endpoint: str | None = None
    # Fraction of runs traced; lowering it stretches the free trace allowance.
    langsmith_tracing_sampling_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    # Traces contain user messages, prompts and issue text. Hiding them keeps
    # only structure, timings and metadata in LangSmith.
    langsmith_hide_inputs: bool = False
    langsmith_hide_outputs: bool = False


def configure_tracing(settings: TracingSettings | None = None) -> bool:
    """Export LangSmith settings to the environment; return whether tracing is on."""

    settings = settings or TracingSettings()
    if settings.langsmith_tracing and settings.langsmith_api_key is None:
        LOGGER.warning("LANGSMITH_TRACING is true but LANGSMITH_API_KEY is not set; tracing stays off")
    enabled = settings.langsmith_tracing and settings.langsmith_api_key is not None
    # Set explicitly either way, so a stale exported value cannot turn tracing on.
    os.environ["LANGSMITH_TRACING"] = "true" if enabled else "false"
    if not enabled:
        return False
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key.get_secret_value()
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    os.environ["LANGSMITH_TRACING_SAMPLING_RATE"] = str(
        settings.langsmith_tracing_sampling_rate
    )
    os.environ["LANGSMITH_HIDE_INPUTS"] = str(settings.langsmith_hide_inputs).lower()
    os.environ["LANGSMITH_HIDE_OUTPUTS"] = str(settings.langsmith_hide_outputs).lower()
    if settings.langsmith_endpoint:
        os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
    LOGGER.info(
        "LangSmith tracing enabled project=%s sampling=%s hide_inputs=%s hide_outputs=%s",
        settings.langsmith_project,
        settings.langsmith_tracing_sampling_rate,
        settings.langsmith_hide_inputs,
        settings.langsmith_hide_outputs,
    )
    return True
