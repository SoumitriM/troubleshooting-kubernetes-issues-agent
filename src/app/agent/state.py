from typing import Any, Literal, TypedDict
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


Route = Literal["generate_fix", "generate_findings", "request_clarification"]


class QueryAnalysis(BaseModel):
    """Single validated handoff from incident analysis to GraphRAG."""

    model_config = ConfigDict(extra="forbid")

    symptom: str | None = None
    components: list[str] = Field(default_factory=list)
    kubernetes_version: str | None = None
    error_messages: list[str] = Field(default_factory=list)
    has_sufficient_details: bool
    missing_details: list[str] = Field(default_factory=list)


class IncidentState(TypedDict, total=False):
    original_query: str
    user_query: str
    clarification_history: list[str]
    clarification_attempts: int
    clarification_exhausted: bool
    debug: bool
    query_analysis: QueryAnalysis
    extracted_symptom: str | None
    extracted_components: list[str]
    kubernetes_version: str | None
    error_messages: list[str]
    has_sufficient_details: bool
    missing_details: list[str]
    retrieved_context: dict[str, Any]
    top_similarity_score: float | None
    actionable_fix_available: bool
    route: Route
    route_reason: str
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
