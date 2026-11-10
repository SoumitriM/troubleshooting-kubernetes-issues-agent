import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agent.state import Intent, Route


class IncidentAnalyzeRequest(BaseModel):
    """Validated user-visible incident analysis request."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4000)
    thread_id: str | None = None

    @field_validator("query")
    @classmethod
    def validate_query(cls, query: str) -> str:
        normalized = query.strip()
        if not normalized:
            raise ValueError("query must not be empty")
        return normalized

    @field_validator("thread_id")
    @classmethod
    def validate_thread_id(cls, thread_id: str | None) -> str | None:
        if thread_id is None:
            return None
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", thread_id):
            raise ValueError("thread_id contains unsupported characters")
        return thread_id


class IncidentAnalyzeResponse(BaseModel):
    """Safe public projection of the LangGraph result."""

    model_config = ConfigDict(extra="forbid")

    route: Route
    answer: str
    citations: list[str] = Field(default_factory=list)


class IncidentResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1, max_length=4000)

    @field_validator("answer")
    @classmethod
    def validate_answer(cls, answer: str) -> str:
        normalized = answer.strip()
        if not normalized:
            raise ValueError("answer must not be empty")
        return normalized


class IncidentClarificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str
    status: Literal["needs_clarification"]
    question: str
    known_details: dict[str, Any]
    missing_details: list[str]


class IncidentCompletedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str
    status: Literal["completed"]
    route: Route
    answer: str
    citations: list[str] = Field(default_factory=list)


IncidentConversationResponse = (
    IncidentClarificationResponse | IncidentCompletedResponse
)


class KnowledgeQueryRequest(IncidentAnalyzeRequest):
    """Query accepted by the general Kubernetes assistant."""


class KnowledgeQueryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str
    status: Literal["completed", "needs_clarification"]
    intent: Intent
    route: Route
    answer: str
    citations: list[str] = Field(default_factory=list)


class ChatRequest(BaseModel):
    """One user message, optionally continuing an existing conversation."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = None

    @field_validator("message")
    @classmethod
    def validate_message(cls, message: str) -> str:
        normalized = message.strip()
        if not normalized:
            raise ValueError("message must not be empty")
        return normalized

    @field_validator("conversation_id")
    @classmethod
    def validate_conversation_id(cls, conversation_id: str | None) -> str | None:
        if conversation_id is None:
            return None
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", conversation_id):
            raise ValueError("conversation_id contains unsupported characters")
        return conversation_id


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str
    status: Literal["completed", "needs_clarification"] | None = None
    intent: Intent | None = None
    route: Route | None = None
    citations: list[str] = Field(default_factory=list)
    created_at: str


class ChatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    message: ChatMessage


class ConversationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    created_at: str
    updated_at: str


class ConversationDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    messages: list[ChatMessage]
