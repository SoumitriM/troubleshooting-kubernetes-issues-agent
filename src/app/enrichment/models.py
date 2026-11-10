from pydantic import BaseModel, ConfigDict, Field


class IssueEnrichment(BaseModel):
    """Structured facts extracted from an issue by Claude."""

    model_config = ConfigDict(extra="forbid")

    summary: str | None = None
    symptom: str | None = None
    expected_behavior: str | None = None
    components: list[str] = Field(default_factory=list)
    error_messages: list[str] = Field(default_factory=list)
    kubernetes_versions: list[str] = Field(default_factory=list)
