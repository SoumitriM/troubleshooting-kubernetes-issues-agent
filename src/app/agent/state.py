from typing import Any, Literal, TypedDict
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


Intent = Literal[
    "INCIDENT_TROUBLESHOOTING",
    "GRAPH_ANALYTICS",
    "GENERAL_KUBERNETES_QUESTION",
    "NEEDS_CLARIFICATION",
]
Route = Literal[
    "generate_fix",
    "generate_findings",
    "answer_from_graph",
    "general_answer",
    "request_clarification",
]


class NormalizedQuery(BaseModel):
    """Meaning-preserving query representation shared by all routes."""

    model_config = ConfigDict(extra="forbid")

    normalized_query: str = Field(min_length=1)
    components: list[str] = Field(default_factory=list)
    versions: list[str] = Field(default_factory=list)
    symptoms: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)


class IntentClassification(BaseModel):
    """Validated intent-router decision."""

    model_config = ConfigDict(extra="forbid")

    intent: Intent
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1, max_length=300)


class CypherGeneration(BaseModel):
    """Structured read-only Cypher proposed by the model."""

    model_config = ConfigDict(extra="forbid")

    cypher: str = Field(min_length=1)
    parameters: dict[str, str | int | float | bool | list[str]] = Field(
        default_factory=dict
    )
    explanation: str = Field(min_length=1, max_length=500)


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
    normalized_query: str
    intent: Intent
    intent_confidence: float
    intent_reasoning: str
    entities: list[str]
    generated_cypher: str
    cypher_parameters: dict[str, Any]
    cypher_explanation: str
    cypher_validation_errors: list[str]
    graph_results: list[dict[str, Any]]
    cypher_attempt_count: int
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
