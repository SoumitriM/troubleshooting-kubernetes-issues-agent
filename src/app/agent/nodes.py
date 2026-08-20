import re
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agent.state import IncidentState, Route
from app.enrichment.fh_genie_client import FHGenieClient
from app.retrieval.graph_retriever import GraphRetriever


class AgentSettings(BaseSettings):
    """Deterministic routing thresholds loaded from environment or .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    agent_relevance_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    agent_actionable_fix_threshold: float = Field(default=0.65, ge=0.0, le=1.0)


class IncidentNodes:
    """LangGraph node implementations with injected external dependencies."""

    def __init__(
        self,
        fh_genie: FHGenieClient,
        retriever: GraphRetriever,
        settings: AgentSettings,
    ) -> None:
        self._fh_genie = fh_genie
        self._retriever = retriever
        self._settings = settings

    def analyze_incident(self, state: IncidentState) -> dict[str, Any]:
        facts = self._fh_genie.enrich(state["user_query"], None)
        symptom = facts.symptom
        specific_symptom = bool(symptom and not self._is_generic(symptom))
        error_messages = [
            message
            for message in facts.error_messages
            if message and not self._is_generic(message)
        ]
        query_error_signals = re.findall(
            r"\b(?:[45]\d\d|0x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]+Error|"
            r"CrashLoopBackOff|[A-Z][A-Z0-9_-]*\d+[A-Z0-9_-]*)\b",
            state["user_query"],
        )
        error_messages = self._unique([*error_messages, *query_error_signals])
        version = facts.kubernetes_versions[0] if facts.kubernetes_versions else None
        behavior = self._has_problematic_behavior(state["user_query"])
        has_sufficient_details = bool(
            error_messages
            or specific_symptom
            or (facts.components and behavior)
            or (version and behavior)
        )
        missing_details: list[str] = []
        if not specific_symptom and not behavior:
            missing_details.append("specific observed behaviour")
        if not error_messages:
            missing_details.append("exact error message or error code")
        if not facts.components:
            missing_details.append("affected component")
        if not version:
            missing_details.append("Kubernetes version")
        if not re.search(r"\b(log|logs|stack trace|traceback)\b", state["user_query"], re.I):
            missing_details.append("relevant logs")
        return {
            "extracted_symptom": symptom if specific_symptom else None,
            "extracted_components": facts.components,
            "kubernetes_version": version,
            "error_messages": error_messages,
            "has_sufficient_details": has_sufficient_details,
            "missing_details": missing_details,
        }

    def retrieve_evidence(self, state: IncidentState) -> dict[str, Any]:
        context = self._retriever.retrieve(
            state["user_query"],
            5,
            components=state.get("extracted_components", []),
            kubernetes_version=state.get("kubernetes_version"),
            error_messages=state.get("error_messages", []),
            symptom=state.get("extracted_symptom"),
        )
        scores = [
            float(issue["score"])
            for issue in context.get("matched_issues", [])
            if issue.get("score") is not None
        ]
        return {
            "retrieved_context": context,
            "top_similarity_score": max(scores) if scores else None,
        }

    def assess_evidence(self, state: IncidentState) -> dict[str, Any]:
        context = state.get("retrieved_context", {})
        matched = context.get("matched_issues", [])
        score = state.get("top_similarity_score")
        relevant = bool(
            matched
            and score is not None
            and score >= self._settings.agent_relevance_threshold
            and self._has_evidence_overlap(state, matched)
        )
        actionable = self._actionable_fixes(context)
        if (
            relevant
            and score is not None
            and score >= self._settings.agent_actionable_fix_threshold
            and actionable
        ):
            route: Route = "generate_fix"
        elif relevant:
            route = "generate_findings"
        else:
            route = "request_clarification"
        result: dict[str, Any] = {
            "actionable_fix_available": route == "generate_fix",
            "route": route,
        }
        if route == "request_clarification":
            missing = list(state.get("missing_details", []))
            if context.get("no_close_match"):
                missing = self._unique(
                    [*missing, "relevant logs", "an exact error message"]
                )
            if not missing:
                missing = ["additional logs or an exact error message"]
            result["missing_details"] = missing
        return result

    def generate_fix(self, state: IncidentState) -> dict[str, Any]:
        fixes = self._actionable_fixes(state.get("retrieved_context", {}))
        generated = self._fh_genie.generate_grounded_fix(
            state["user_query"],
            fixes,
        )
        steps = "\n".join(f"- {step}" for step in generated.verified_steps)
        return {
            "answer": f"{generated.answer}\nVerified steps:\n{steps}",
            "citations": generated.citations,
        }

    def generate_findings(self, state: IncidentState) -> dict[str, Any]:
        context = state.get("retrieved_context", {})
        matched = context.get("matched_issues", [])[:5]
        related = context.get("related_issues", [])[:3]
        answer_parts = [
            "I found relevant incident records, but no verified actionable fix was found."
        ]
        if matched:
            answer_parts.append(
                "Direct semantic matches:\n"
                + "\n".join(self._format_finding(issue) for issue in matched)
            )
        if related:
            answer_parts.append(
                "Graph-expanded related issues (not direct semantic matches):\n"
                + "\n".join(self._format_finding(issue) for issue in related)
            )
        citations = self._unique(
            url
            for issue in [*matched, *related]
            for url in self._finding_urls(issue)
        )
        return {"answer": "\n".join(answer_parts), "citations": citations}

    def request_clarification(self, state: IncidentState) -> dict[str, Any]:
        missing = state.get("missing_details", [])
        prefix = (
            "No close historical incident was found. Please provide: "
            if state.get("retrieved_context", {}).get("no_close_match")
            else "Please provide: "
        )
        return {
            "answer": prefix + ", ".join(self._unique(missing)) + ".",
            "citations": [],
            "route": "request_clarification",
            "actionable_fix_available": False,
        }

    def _has_evidence_overlap(
        self,
        state: IncidentState,
        matched: list[dict[str, Any]],
    ) -> bool:
        extracted_components = {
            component.strip().lower()
            for component in state.get("extracted_components", [])
            if component.strip()
        }
        version = (state.get("kubernetes_version") or "").strip().lower()
        query_tokens = self._semantic_tokens(
            " ".join(
                filter(
                    None,
                    [state.get("user_query"), state.get("extracted_symptom")],
                )
            )
        )
        for issue in matched:
            relevance = issue.get("relevance") or {}
            if relevance.get("accepted"):
                return True
            issue_components = {
                component.strip().lower()
                for component in issue.get("components", [])
                if component.strip()
            }
            issue_versions = {
                item.strip().lower()
                for item in issue.get("kubernetes_versions", [])
                if item.strip()
            }
            if extracted_components & issue_components:
                return True
            if version and version in issue_versions:
                return True
            issue_tokens = self._semantic_tokens(
                f"{issue.get('title') or ''} {issue.get('summary') or ''}"
            )
            overlap = query_tokens & issue_tokens
            if len(overlap) >= 2 or any(len(token) >= 10 for token in overlap):
                return True
        return False

    @classmethod
    def _is_generic(cls, value: str) -> bool:
        normalized = " ".join(re.findall(r"[a-z0-9]+", value.lower()))
        if normalized in cls.GENERIC_DESCRIPTIONS:
            return True
        return len(normalized.split()) <= 6 and any(
            phrase in normalized for phrase in cls.GENERIC_DESCRIPTIONS
        )

    @classmethod
    def _has_problematic_behavior(cls, value: str) -> bool:
        normalized = " ".join(re.findall(r"[a-z0-9]+", value.lower()))
        if normalized in cls.GENERIC_DESCRIPTIONS:
            return False
        tokens = set(normalized.split())
        if tokens & cls.BEHAVIOR_TERMS:
            return True
        return bool(re.search(r"\b(crashloopbackoff|error\s*\d+|[45]\d\d)\b", normalized))

    @classmethod
    def _semantic_tokens(cls, value: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-z0-9]+", value.lower())
            if len(token) >= 3 and token not in cls.SEMANTIC_STOP_WORDS
        }

    @staticmethod
    def _format_finding(issue: dict[str, Any]) -> str:
        components = issue.get("components") or issue.get("shared_components") or []
        component_text = ", ".join(components[:5]) if components else "not specified"
        description = issue.get("summary") or issue.get("title") or "Similar incident"
        return (
            f"- #{issue.get('issue_number')}: {description} "
            f"(components: {component_text})"
        )

    @classmethod
    def _actionable_fixes(cls, context: dict[str, Any]) -> list[dict[str, Any]]:
        issues = [
            *context.get("matched_issues", []),
            *context.get("related_issues", []),
        ]
        fixes: list[dict[str, Any]] = []
        for issue in issues:
            disposition = issue.get("verified_disposition") or {}
            fix = issue.get("actionable_fix")
            if not fix or not disposition.get("disposition_verified"):
                continue
            if not disposition.get("actionable_fix_available"):
                continue
            if disposition.get("resolution_type") not in {"fixed", "configuration"}:
                continue
            if not fix.get("verified_steps") or not fix.get("evidence_urls"):
                continue
            fixes.append(fix)
        return fixes

    @staticmethod
    def _finding_urls(issue: dict[str, Any]) -> list[str]:
        # Findings describe the issue itself, not its resolution evidence. Resolution
        # URLs are cited only by generate_fix when the answer actually uses that fix.
        return [issue["url"]] if issue.get("url") else []

    @staticmethod
    def _unique(values: Any) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))
    GENERIC_DESCRIPTIONS = {
        "not working",
        "broken",
        "has an issue",
        "there is an error",
        "something is wrong",
        "kubernetes problem",
        "kubernetes is not working",
        "something is broken",
    }
    BEHAVIOR_TERMS = {
        "allow",
        "bypass",
        "cannot",
        "crash",
        "denied",
        "enable",
        "fail",
        "hang",
        "leak",
        "pending",
        "restart",
        "stuck",
        "timeout",
        "unable",
    }
    SEMANTIC_STOP_WORDS = {
        "after",
        "and",
        "are",
        "can",
        "for",
        "from",
        "into",
        "issue",
        "kubernetes",
        "not",
        "that",
        "the",
        "this",
        "users",
        "with",
    }
