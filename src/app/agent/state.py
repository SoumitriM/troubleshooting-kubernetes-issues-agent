from typing import Any, Literal, TypedDict
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


Route = Literal["generate_fix", "generate_findings", "request_clarification"]


class IncidentState(TypedDict, total=False):
    user_query: str
    extracted_symptom: str | None
    extracted_components: list[str]
    kubernetes_version: str | None
    retrieved_context: dict[str, Any]
    top_similarity_score: float | None
    actionable_fix_available: bool
    route: Route
    answer: str
    citations: list[str]


class GroundedFixAnswer(BaseModel):
    """Validated FH Genie output constrained to retrieved evidence."""

    model_config = ConfigDict(extra="forbid")

    answer: str
    verified_steps: list[str] = Field(min_length=1)
    citations: list[str] = Field(min_length=1)

    @field_validator("citations")
    @classmethod
    def validate_citations(cls, citations: list[str]) -> list[str]:
        for citation in citations:
            parsed = urlparse(citation)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("citations must be absolute HTTP URLs")
        return list(dict.fromkeys(citations))
