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
        return {
            "extracted_symptom": facts.symptom,
            "extracted_components": facts.components,
            "kubernetes_version": (
                facts.kubernetes_versions[0] if facts.kubernetes_versions else None
            ),
        }

    def retrieve_evidence(self, state: IncidentState) -> dict[str, Any]:
        context = self._retriever.retrieve(state["user_query"], 5)
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
        return {
            "actionable_fix_available": route == "generate_fix",
            "route": route,
        }

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
        issues = context.get("matched_issues", [])[:5]
        components = self._unique(
            component for issue in issues for component in issue.get("components", [])
        )
        descriptions = [
            f"#{issue.get('issue_number')}: "
            f"{issue.get('summary') or issue.get('title') or 'Similar incident'}"
            for issue in issues
        ]
        answer_parts = [
            "I found relevant incident records, but no verified actionable fix was found."
        ]
        if components:
            answer_parts.append(f"Affected components: {', '.join(components)}.")
        if descriptions:
            answer_parts.append("Similar incidents: " + "; ".join(descriptions))
        citations = self._unique(
            url
            for issue in issues
            for url in self._finding_urls(issue)
        )
        return {"answer": "\n".join(answer_parts), "citations": citations}

    def request_clarification(self, state: IncidentState) -> dict[str, Any]:
        missing: list[str] = []
        if not state.get("kubernetes_version"):
            missing.append("Kubernetes version")
        if not state.get("extracted_components"):
            missing.append("affected component")
        missing.extend(["relevant logs", "exact error message"])
        return {
            "answer": "I could not find sufficiently similar evidence. Please provide: "
            + ", ".join(self._unique(missing))
            + ".",
            "citations": [],
        }

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
        urls = [issue.get("url")]
        disposition = issue.get("verified_disposition") or {}
        urls.extend(disposition.get("evidence_urls", []))
        return [url for url in urls if url]

    @staticmethod
    def _unique(values: Any) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))
