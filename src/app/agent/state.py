from typing import Any, Literal, TypedDict
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


Intent = Literal[
    "INCIDENT_TROUBLESHOOTING",
    "GRAPH_ANALYTICS",
    "GENERAL_KUBERNETES_QUESTION",
    "NEEDS_CLARIFICATION",
]
AnalyticsOperation = Literal["COUNT", "GROUP_BY", "TOP_N", "RELATIONSHIP", "LIST"]
AnalyticsGroup = Literal["component", "version", "issue"]
EvidenceSource = Literal["incidents", "analytics", "docs"]
Route = Literal[
    "research_answer",
    "generate_fix",
    "generate_findings",
    "docs_troubleshooting",
    "answer_from_graph",
    "general_answer",
    "request_clarification",
]

MAX_SUGGESTIONS = 3
MAX_SUGGESTION_CHARS = 100


def clean_suggestions(suggestions: list[str]) -> list[str]:
    """Trim, de-duplicate and cap follow-up suggestions shown as quick replies."""

    cleaned: list[str] = []
    for item in suggestions:
        text = " ".join(item.split())
        if not text:
            continue
        if len(text) > MAX_SUGGESTION_CHARS:
            raise ValueError(
                f"each suggestion must be at most {MAX_SUGGESTION_CHARS} characters"
            )
        if "http://" in text or "https://" in text:
            raise ValueError("suggestions must not contain URLs")
        if text.casefold() not in {existing.casefold() for existing in cleaned}:
            cleaned.append(text)
    return cleaned[:MAX_SUGGESTIONS]


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


class QueryUnderstanding(BaseModel):
    """One model-produced semantic interpretation reused by every route."""

    model_config = ConfigDict(extra="forbid")

    normalized_query: str = Field(min_length=1)
    intent: Intent
    intent_confidence: float = Field(ge=0.0, le=1.0)
    components: list[str] = Field(default_factory=list)
    versions: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    symptoms: list[str] = Field(default_factory=list)
    error_messages: list[str] = Field(default_factory=list)
    observed_behavior: str | None = None
    has_concrete_incident: bool = False
    # False for non-Kubernetes questions and for requests to change data or
    # run commands; such queries get a scope answer instead of clarification.
    in_scope: bool = True
    # False when the latest "Clarification N:" answer does not answer the
    # pending question: off-topic, destructive, or instructions to the assistant.
    clarification_answer_relevant: bool = True
    # Evidence sources the question needs; two or more route to research.
    sources: list[EvidenceSource] = Field(default_factory=list)
    # Analytics conditions the component/version templates cannot express
    # ("has a verified fix", "linked to a pull request", "closed in July").
    extra_conditions: list[str] = Field(default_factory=list)
    reasoning: str = Field(min_length=1, max_length=500)
    analytics_operation: AnalyticsOperation | None = None
    group_by: AnalyticsGroup | None = None
    limit: int | None = Field(default=None, ge=1, le=100)


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
    # Earlier turns of a chat conversation ({"role", "content"}), used only to
    # resolve follow-up references when interpreting the current query.
    conversation_history: list[dict[str, str]]
    # Running summary of the chat's current problem (IncidentMemory as a
    # dict), so details from turns older than the history window are kept.
    incident_memory: dict[str, Any]
    debug: bool
    query_analysis: QueryAnalysis
    query_understanding: QueryUnderstanding
    query_understanding_unavailable: bool
    out_of_scope: bool
    evidence_unavailable: bool
    normalized_query: str
    normalized_symptoms: list[str]
    intent: Intent
    intent_confidence: float
    intent_reasoning: str
    entities: list[str]
    generated_cypher: str
    cypher_parameters: dict[str, Any]
    cypher_explanation: str
    cypher_validation_errors: list[str]
    graph_results: list[dict[str, Any]]
    graph_display_limit: int | None
    graph_limit_probed: bool
    cypher_source: str
    alternative_attempted: bool
    alternative_hints: list[str]
    research_steps: list[dict[str, Any]]
    research_failed: bool
    documentation_results: list[dict[str, Any]]
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
    # Short follow-ups the UI offers as one-click replies.
    suggestions: list[str]
    # Conversational question prepared before the clarification interrupt, so
    # the model call is not repeated when LangGraph re-runs the paused node.
    clarification_question: str
    # No reported issue matched and the docs could not answer either, so the
    # turn falls back to a clarifying question.
    docs_troubleshooting_failed: bool
    # A clarification answer rejected as off-topic or unsafe; it is removed
    # from clarification_history and declined instead of merged.
    rejected_message: str


class IncidentMatchJudgement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_number: int
    relation: Literal["same_failure", "related", "unrelated"]


class IncidentMatchVerification(BaseModel):
    """Model labels for retrieved candidates; may only narrow retrieval."""

    model_config = ConfigDict(extra="forbid")

    judgements: list[IncidentMatchJudgement]


class ResearchStep(BaseModel):
    """One step of the bounded multi-source research loop."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["search_incidents", "graph_analytics", "search_docs", "final_answer"]
    reason: str = Field(min_length=1, max_length=300)
    query: str | None = None
    operation: AnalyticsOperation | None = None
    group_by: AnalyticsGroup | None = None
    components: list[str] = Field(default_factory=list)
    versions: list[str] = Field(default_factory=list)
    answer: str | None = None
    citations: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)

    @field_validator("suggestions")
    @classmethod
    def validate_suggestions(cls, suggestions: list[str]) -> list[str]:
        return clean_suggestions(suggestions)


class GroundedFixAnswer(BaseModel):
    """Validated model output constrained to retrieved evidence."""

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


class ComposedReply(BaseModel):
    """A conversational reply written around evidence a route already gathered.

    The reply may rephrase supplied facts but never add new ones; the client
    checks every number, code span, issue reference and URL against the payload.
    """

    model_config = ConfigDict(extra="forbid")

    reply: str = Field(min_length=1, max_length=2000)
    citations: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)

    @field_validator("citations")
    @classmethod
    def unique_citations(cls, citations: list[str]) -> list[str]:
        return list(dict.fromkeys(citations))

    @field_validator("suggestions")
    @classmethod
    def validate_suggestions(cls, suggestions: list[str]) -> list[str]:
        return clean_suggestions(suggestions)


class DocsTroubleshootingAnswer(BaseModel):
    """Likely causes for an unmatched incident, taken from documentation chunks."""

    model_config = ConfigDict(extra="forbid")

    answerable: bool
    answer: str = Field(default="", max_length=4000)
    citations: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)

    @field_validator("citations")
    @classmethod
    def unique_citations(cls, citations: list[str]) -> list[str]:
        return list(dict.fromkeys(citations))

    @field_validator("suggestions")
    @classmethod
    def validate_suggestions(cls, suggestions: list[str]) -> list[str]:
        return clean_suggestions(suggestions)


MEMORY_ITEM_CHARS = 200
MEMORY_LIST_ITEMS = 10


class IncidentMemory(BaseModel):
    """Facts gathered about the user's current problem across a whole chat.

    Every value comes from the user's own messages or the assistant's
    evidence-backed replies; the client rejects details absent from both.
    """

    model_config = ConfigDict(extra="forbid")

    problem: str | None = None
    components: list[str] = Field(default_factory=list)
    versions: list[str] = Field(default_factory=list)
    error_messages: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)
    ruled_out: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    open_question: str | None = None

    @field_validator("problem", "open_question")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = " ".join(value.split())
        if len(text) > MEMORY_ITEM_CHARS * 2:
            raise ValueError(f"keep it under {MEMORY_ITEM_CHARS * 2} characters")
        return text or None

    @field_validator(
        "components", "versions", "error_messages", "observations", "ruled_out", "findings"
    )
    @classmethod
    def validate_items(cls, items: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(" ".join(item.split()) for item in items if item.strip()))
        if len(cleaned) > MEMORY_LIST_ITEMS:
            raise ValueError(f"keep at most {MEMORY_LIST_ITEMS} items per list")
        for item in cleaned:
            if len(item) > MEMORY_ITEM_CHARS:
                raise ValueError(f"keep each item under {MEMORY_ITEM_CHARS} characters")
        return cleaned

    def is_empty(self) -> bool:
        return not any(self.model_dump().values())
