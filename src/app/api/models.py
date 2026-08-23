from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agent.state import Route


class IncidentAnalyzeRequest(BaseModel):
    """Validated user-visible incident analysis request."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4000)

    @field_validator("query")
    @classmethod
    def validate_query(cls, query: str) -> str:
        normalized = query.strip()
        if not normalized:
            raise ValueError("query must not be empty")
        return normalized


class IncidentAnalyzeResponse(BaseModel):
    """Safe public projection of the LangGraph result."""

    model_config = ConfigDict(extra="forbid")

    route: Route
    answer: str
    citations: list[str] = Field(default_factory=list)
