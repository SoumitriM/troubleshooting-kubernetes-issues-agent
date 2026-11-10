import json
import logging
import subprocess
from collections.abc import Callable
from typing import Any, Literal, TypeVar

import anthropic
from anthropic import transform_schema
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agent.state import (
    CypherGeneration,
    GroundedFixAnswer,
    IncidentMatchVerification,
    IntentClassification,
    NormalizedQuery,
    QueryUnderstanding,
    ResearchStep,
)
from app.documentation.models import GroundedDocumentationAnswer
from app.enrichment.models import IssueEnrichment
from app.enrichment.prompts import (
    CYPHER_SYSTEM_PROMPT,
    DOCUMENTATION_ANSWER_SYSTEM_PROMPT,
    FIX_ANSWER_SYSTEM_PROMPT,
    INCIDENT_MATCH_SYSTEM_PROMPT,
    INTENT_SYSTEM_PROMPT,
    NORMALIZATION_SYSTEM_PROMPT,
    QUERY_UNDERSTANDING_SYSTEM_PROMPT,
    RESEARCH_SYSTEM_PROMPT,
    RESOLUTION_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
)
from app.enrichment.resolution_models import IssueResolution


LOGGER = logging.getLogger(__name__)

# Server-side refusal fallback: a declined request is re-run on a model chosen
# by refusal category inside the same call.
REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_OUTPUT_TOKENS = 16000

OutputT = TypeVar("OutputT", bound=BaseModel)


class ClaudeSettings(BaseSettings):
    """Claude configuration loaded from environment variables and .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM_* rather than CLAUDE_* names: Claude tooling exports variables such as
    # CLAUDE_EFFORT into child processes, which would silently override these.
    # Optional: without it the SDK resolves ANTHROPIC_AUTH_TOKEN or an
    # `ant auth login` profile.
    anthropic_api_key: SecretStr | None = None
    # "claude_code" runs each call through headless Claude Code (`claude -p`),
    # which reuses its login instead of an API key. For local development only.
    llm_backend: Literal["api", "claude_code"] = "api"
    llm_claude_code_path: str = "claude"
    llm_model: str = "claude-opus-5-5"
    # Every call is short structured extraction or classification, which holds
    # quality at low effort; raise it if the golden evaluation regresses.
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=1, ge=0, le=5)
    llm_refusal_fallback: bool = True


class EnrichmentResponseError(ValueError):
    """A model response failed safe validation without retaining its content."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CypherParameter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    value: str | int | float | bool | list[str]


class CypherGenerationOutput(BaseModel):
    """Wire format for Cypher generation.

    Structured outputs require ``additionalProperties: false`` on every object, so
    a free-form parameter mapping is sent as a list of name/value pairs.
    """

    model_config = ConfigDict(extra="forbid")

    cypher: str = Field(min_length=1)
    parameters: list[CypherParameter] = Field(default_factory=list)
    explanation: str = Field(min_length=1, max_length=500)


class ClaudeClient:
    """Run every structured model call through Claude with validated outputs."""

    def __init__(
        self,
        settings: ClaudeSettings,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self._model = settings.llm_model
        self._effort = settings.llm_effort
        self._refusal_fallback = settings.llm_refusal_fallback
        self._backend = settings.llm_backend
        self._claude_code_path = settings.llm_claude_code_path
        self._timeout = settings.llm_timeout_seconds
        api_key = (
            settings.anthropic_api_key.get_secret_value()
            if settings.anthropic_api_key
            else None
        )
        self._client = client or anthropic.Anthropic(
            api_key=api_key,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )

    def enrich(self, title: str, body: str | None) -> IssueEnrichment:
        """Extract structured facts from one issue."""

        return self._structured(
            SYSTEM_PROMPT, {"title": title, "body": body}, IssueEnrichment
        )

    def extract_resolution(self, issue: dict[str, Any]) -> IssueResolution:
        """Extract a grounded resolution that cites only supplied evidence URLs."""

        evidence = issue.get("resolution_evidence") or {}
        payload = {
            "title": issue.get("title"),
            "body": issue.get("body"),
            "comments": evidence.get("comments", []),
            "linked_pull_requests": evidence.get("linked_pull_requests", []),
            "closing_commits": evidence.get("closing_commits", []),
        }
        supplied_urls = self._collect_supplied_urls(payload)

        def check(resolution: IssueResolution) -> str | None:
            if not set(resolution.evidence_urls).issubset(supplied_urls):
                return "response contains unsupported evidence URL"
            return None

        return self._structured(
            RESOLUTION_SYSTEM_PROMPT, payload, IssueResolution, check
        )

    def generate_grounded_fix(
        self,
        user_query: str,
        actionable_fixes: list[dict[str, Any]],
    ) -> GroundedFixAnswer:
        """Generate a fix answer constrained to exact retrieved evidence."""

        allowed_steps = {
            step for fix in actionable_fixes for step in fix.get("verified_steps", [])
        }
        allowed_urls = {
            url for fix in actionable_fixes for url in fix.get("evidence_urls", [])
        }
        allowed_summaries = {
            summary
            for fix in actionable_fixes
            if (summary := fix.get("resolution_summary"))
        }

        def check(answer: GroundedFixAnswer) -> str | None:
            if answer.answer not in allowed_summaries:
                return "response contains unsupported fix summary"
            if not set(answer.verified_steps).issubset(allowed_steps):
                return "response contains unsupported fix step"
            if not set(answer.citations).issubset(allowed_urls):
                return "response contains unsupported citation"
            return None

        return self._structured(
            FIX_ANSWER_SYSTEM_PROMPT,
            {"user_query": user_query, "actionable_fixes": actionable_fixes},
            GroundedFixAnswer,
            check,
        )

    def verify_incident_matches(
        self, report: str, candidates: list[dict[str, Any]]
    ) -> dict[int, str]:
        """Label each retrieved candidate's relation to the reported incident."""

        expected = {candidate["issue_number"] for candidate in candidates}

        def check(output: IncidentMatchVerification) -> str | None:
            numbers = [item.issue_number for item in output.judgements]
            if len(numbers) != len(set(numbers)) or set(numbers) != expected:
                return "label every supplied issue_number exactly once"
            return None

        output = self._structured(
            INCIDENT_MATCH_SYSTEM_PROMPT,
            {"incident_report": report, "candidates": candidates},
            IncidentMatchVerification,
            check,
        )
        return {item.issue_number: item.relation for item in output.judgements}

    def research_step(
        self,
        question: str,
        observations: list[dict[str, Any]],
        *,
        remaining_tool_calls: int,
        allowed_urls: set[str],
    ) -> ResearchStep:
        """Choose the next read-only tool call, or answer from the observations."""

        def check(step: ResearchStep) -> str | None:
            if step.action == "final_answer":
                if not step.answer:
                    return "final_answer requires an answer"
                if not set(step.citations).issubset(allowed_urls):
                    return "response contains unsupported citation"
                return None
            if remaining_tool_calls <= 0:
                return "no tool calls remain; return final_answer"
            if step.action in {"search_incidents", "search_docs"} and not step.query:
                return f"{step.action} requires a query"
            if step.action == "graph_analytics" and not step.operation:
                return "graph_analytics requires an operation"
            return None

        return self._structured(
            RESEARCH_SYSTEM_PROMPT,
            {
                "question": question,
                "remaining_tool_calls": remaining_tool_calls,
                "observations": observations,
            },
            ResearchStep,
            check,
        )

    def normalize_query(self, query: str) -> NormalizedQuery:
        return self._structured(
            NORMALIZATION_SYSTEM_PROMPT, {"query": query}, NormalizedQuery
        )

    def analyze_query(
        self, query: str, history: list[dict[str, str]] | None = None
    ) -> QueryUnderstanding:
        """Understand a runtime query in one validated, retryable model call.

        ``history`` holds earlier chat turns so follow-up questions can be
        resolved into a standalone interpretation.
        """

        payload: dict[str, Any] = {"query": query}
        if history:
            payload["conversation_history"] = history
        return self._structured(
            QUERY_UNDERSTANDING_SYSTEM_PROMPT, payload, QueryUnderstanding
        )

    def classify_query(self, normalized: NormalizedQuery) -> IntentClassification:
        return self._structured(
            INTENT_SYSTEM_PROMPT, normalized.model_dump(), IntentClassification
        )

    def generate_cypher(
        self,
        normalized: NormalizedQuery,
        *,
        intent: str,
        validation_errors: list[str] | None = None,
        hints: list[str] | None = None,
    ) -> CypherGeneration:
        def check(output: CypherGenerationOutput) -> str | None:
            names = [parameter.name for parameter in output.parameters]
            if len(names) != len(set(names)):
                return "parameter names must be unique"
            return None

        output = self._structured(
            CYPHER_SYSTEM_PROMPT,
            {
                "query": normalized.model_dump(),
                "intent": intent,
                "validation_feedback": validation_errors or [],
                "hints": hints or [],
            },
            CypherGenerationOutput,
            check,
        )
        return CypherGeneration(
            cypher=output.cypher,
            parameters={
                parameter.name: parameter.value for parameter in output.parameters
            },
            explanation=output.explanation,
        )

    def answer_documentation_question(
        self, query: str, chunks: list[dict[str, Any]]
    ) -> GroundedDocumentationAnswer:
        allowed_urls = {chunk["source_url"] for chunk in chunks}

        def check(answer: GroundedDocumentationAnswer) -> str | None:
            if not set(answer.citations).issubset(allowed_urls):
                return "response contains unsupported citation"
            return None

        return self._structured(
            DOCUMENTATION_ANSWER_SYSTEM_PROMPT,
            {"query": query, "documentation_chunks": chunks},
            GroundedDocumentationAnswer,
            check,
        )

    def complete_structured(
        self, prompt: str, payload: Any, output_model: type[OutputT]
    ) -> OutputT:
        """Run any schema-constrained call; used by evaluation tooling."""

        return self._structured(prompt, payload, output_model)

    def _structured(
        self,
        prompt: str,
        payload: Any,
        output_model: type[OutputT],
        check: Callable[[OutputT], str | None] | None = None,
    ) -> OutputT:
        """Request schema-constrained JSON, validate it, and retry once with feedback."""

        content = json.dumps(payload, ensure_ascii=False)
        output_format = {"type": "json_schema", "schema": transform_schema(output_model)}
        failure_reason = "invalid model response"
        feedback = ""
        for _attempt in range(2):
            system = prompt
            if feedback:
                system += (
                    "\nYour previous response was rejected: "
                    + feedback
                    + "\nReturn a corrected response."
                )
            stop_reason, text = self._complete(system, content, output_format)
            if stop_reason == "refusal":
                raise EnrichmentResponseError("model declined the request")
            if stop_reason == "max_tokens":
                failure_reason = feedback = "response was truncated"
                continue
            if not text:
                failure_reason = feedback = "empty model response"
                continue
            try:
                result = output_model.model_validate_json(text)
            except ValidationError as error:
                LOGGER.warning(
                    "Structured Claude response validation failed: %s",
                    [
                        {"location": item["loc"], "type": item["type"]}
                        for item in error.errors()
                    ],
                )
                feedback = str(error)
                failure_reason = (
                    "invalid JSON response"
                    if any(item["type"] == "json_invalid" for item in error.errors())
                    else "response schema validation failed"
                )
                continue
            problem = check(result) if check else None
            if problem:
                failure_reason = feedback = problem
                continue
            return result
        raise EnrichmentResponseError(failure_reason)

    def _complete(
        self, system: str, content: str, output_format: dict[str, Any]
    ) -> tuple[str | None, str]:
        """Run one schema-constrained request and return its stop reason and text."""

        if self._backend == "claude_code":
            return self._complete_with_claude_code(system, content, output_format)
        fallback_options: dict[str, Any] = (
            {"betas": [REFUSAL_FALLBACK_BETA], "fallbacks": "default"}
            if self._refusal_fallback
            else {}
        )
        response = self._client.beta.messages.create(
            model=self._model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": self._effort, "format": output_format},
            **fallback_options,
        )
        text = next(
            (block.text for block in response.content if block.type == "text"), ""
        )
        return response.stop_reason, text

    def _complete_with_claude_code(
        self, system: str, content: str, output_format: dict[str, Any]
    ) -> tuple[str | None, str]:
        # No tools, MCP servers, skills, settings, or saved sessions: each call is
        # a single isolated structured completion.
        completed = subprocess.run(
            [
                self._claude_code_path,
                "--print",
                "--output-format", "json",
                "--model", self._model,
                "--effort", self._effort,
                "--system-prompt", system,
                "--json-schema", json.dumps(output_format["schema"]),
                "--tools", "",
                "--strict-mcp-config",
                "--disable-slash-commands",
                "--setting-sources", "",
                "--no-session-persistence",
            ],
            input=content,
            capture_output=True,
            text=True,
            timeout=self._timeout,
            check=False,
        )
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(
                f"Claude Code exited with status {completed.returncode}"
            ) from None
        if result.get("is_error"):
            raise RuntimeError(
                f"Claude Code request failed: {str(result.get('result'))[:200]}"
            )
        structured = result.get("structured_output")
        if structured is not None:
            return result.get("stop_reason"), json.dumps(structured)
        return result.get("stop_reason"), result.get("result") or ""

    @staticmethod
    def _collect_supplied_urls(value: Any) -> set[str]:
        if isinstance(value, dict):
            return {
                url
                for nested in value.values()
                for url in ClaudeClient._collect_supplied_urls(nested)
            }
        if isinstance(value, list):
            return {
                url
                for nested in value
                for url in ClaudeClient._collect_supplied_urls(nested)
            }
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return {value}
        return set()
