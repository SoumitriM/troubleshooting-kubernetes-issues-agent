import json
from typing import Any, Protocol

from openai import OpenAI
from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.enrichment.models import IssueEnrichment


class EnrichmentSettings(BaseSettings):
    """FH Genie configuration loaded from environment variables and .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    fh_genie_key: SecretStr
    fh_genie_base_url: str
    fh_genie_model: str


class Completions(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class Chat(Protocol):
    completions: Completions


class OpenAICompatibleClient(Protocol):
    chat: Chat


SYSTEM_PROMPT = """You extract supported facts from Kubernetes GitHub issues.
Return exactly one JSON object with these fields: summary, symptom,
expected_behavior, components, error_messages, kubernetes_versions.

Rules:
- Use only information explicitly supported by the supplied title and body.
- Do not infer or invent missing details.
- Use null for unavailable scalar fields.
- Use empty arrays for unavailable list fields.
- Return JSON only, without Markdown fences or commentary.
"""


class FHGenieClient:
    """Extract structured issue facts through FH Genie's compatible API."""

    def __init__(
        self,
        settings: EnrichmentSettings,
        client: OpenAICompatibleClient | None = None,
    ) -> None:
        self._model = settings.fh_genie_model
        self._client = client or OpenAI(
            api_key=settings.fh_genie_key.get_secret_value(),
            base_url=settings.fh_genie_base_url,
        )

    def enrich(self, title: str, body: str | None) -> IssueEnrichment:
        """Enrich one issue, retrying once after an invalid model response."""

        last_error: ValueError | ValidationError | json.JSONDecodeError | None = None
        for _attempt in range(2):
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"Issue title:\n{title}\n\nIssue body:\n{body or ''}",
                    },
                ],
                temperature=0,
            )
            content = response.choices[0].message.content
            try:
                if not content:
                    raise ValueError("FH Genie returned empty content")
                return IssueEnrichment.model_validate_json(content)
            except (ValueError, ValidationError, json.JSONDecodeError) as error:
                last_error = error

        raise ValueError("FH Genie returned an invalid enrichment response") from last_error
