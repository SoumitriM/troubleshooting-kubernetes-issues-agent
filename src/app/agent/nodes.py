import logging
import re
from pathlib import Path
from typing import Any

from langgraph.types import interrupt
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agent.state import (
    CypherGeneration,
    IncidentState,
    NormalizedQuery,
    QueryAnalysis,
    QueryUnderstanding,
    Route,
)
from app.enrichment.fh_genie_client import FHGenieClient
from app.documentation.store import KubernetesDocsRetriever
from app.graph.analytics import GraphAnalyticsRepository
from app.graph.cypher_validation import CypherValidator
from app.retrieval.graph_retriever import GraphRetriever


LOGGER = logging.getLogger(__name__)


class AgentSettings(BaseSettings):
    """Deterministic routing thresholds loaded from environment or .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    agent_relevance_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    agent_actionable_fix_threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    documentation_index_path: Path = Path("data/kubernetes_docs.sqlite")


class IncidentNodes:
    """LangGraph node implementations with injected external dependencies."""

    def __init__(
        self,
        fh_genie: FHGenieClient,
        retriever: GraphRetriever,
        settings: AgentSettings,
        *,
        analytics_repository: GraphAnalyticsRepository | None = None,
        documentation_retriever: KubernetesDocsRetriever | None = None,
        cypher_validator: CypherValidator | None = None,
        enable_interrupts: bool = False,
    ) -> None:
        self._fh_genie = fh_genie
        self._retriever = retriever
        self._settings = settings
        self._analytics_repository = analytics_repository
        self._documentation_retriever = documentation_retriever
        self._cypher_validator = cypher_validator or CypherValidator()
        self._enable_interrupts = enable_interrupts

    def understand_query(self, state: IncidentState) -> dict[str, Any]:
        original_query = state.get("original_query") or state["user_query"]
        history = list(state.get("clarification_history", []))
        query = "\n".join(
            [original_query]
            + [
                f"Clarification {index}: {answer}"
                for index, answer in enumerate(history, 1)
            ]
        )
        try:
            analysis = self._fh_genie.analyze_query(query)
            if not self._preserves_technical_tokens(query, analysis.normalized_query):
                raise ValueError("normalized query lost an immutable technical token")
        except Exception:
            LOGGER.exception("FH Genie query understanding unavailable")
            analysis = QueryUnderstanding(
                normalized_query=query.strip(),
                intent="NEEDS_CLARIFICATION",
                intent_confidence=0.0,
                reasoning="Query understanding service is temporarily unavailable",
            )
        LOGGER.info(
            "Query routed intent=%s confidence=%.2f",
            analysis.intent,
            analysis.intent_confidence,
        )
        symptom = analysis.observed_behavior or next(iter(analysis.symptoms), None)
        missing_details: list[str] = []
        if not symptom:
            missing_details.append("specific observed behaviour")
        if not analysis.error_messages:
            missing_details.append("exact error message or error code")
        if not analysis.components:
            missing_details.append("affected component")
        if not analysis.versions:
            missing_details.append("Kubernetes version")
        missing_details.append("relevant logs")
        return {
            "original_query": original_query,
            "user_query": analysis.normalized_query,
            "normalized_query": analysis.normalized_query,
            "normalized_symptoms": analysis.symptoms,
            "query_understanding": analysis,
            "intent": analysis.intent,
            "intent_confidence": analysis.intent_confidence,
            "intent_reasoning": analysis.reasoning,
            "entities": analysis.entities,
            "extracted_symptom": symptom,
            "extracted_components": analysis.components,
            "kubernetes_version": next(iter(analysis.versions), None),
            "error_messages": analysis.error_messages,
            "has_sufficient_details": analysis.has_concrete_incident,
            "missing_details": missing_details,
            "cypher_attempt_count": 0,
            "cypher_validation_errors": [],
        }

    def generate_cypher(self, state: IncidentState) -> dict[str, Any]:
        attempt = state.get("cypher_attempt_count", 0) + 1
        try:
            normalized = self._normalization_from_state(state)
            generated = self._analytics_cypher(state["query_understanding"])
            source = "deterministic"
            if generated is None:
                source = "model"
                generated = self._fh_genie.generate_cypher(
                    normalized,
                    intent=state["intent"],
                    validation_errors=state.get("cypher_validation_errors", []),
                )
            LOGGER.info(
                "Generated Cypher attempt=%d source=%s",
                attempt,
                source,
            )
            return {
                "generated_cypher": generated.cypher,
                "cypher_parameters": generated.parameters,
                "cypher_explanation": generated.explanation,
                "cypher_attempt_count": attempt,
            }
        except Exception:
            LOGGER.warning("Cypher generation failed attempt=%d", attempt)
            return {
                "generated_cypher": "",
                "cypher_parameters": {},
                "cypher_validation_errors": ["Cypher generation returned invalid output"],
                "cypher_attempt_count": attempt,
            }

    def validate_cypher(self, state: IncidentState) -> dict[str, Any]:
        result = self._cypher_validator.validate(
            state.get("generated_cypher", ""),
            state.get("cypher_parameters", {}),
        )
        LOGGER.info(
            "Cypher validation valid=%s attempt=%d",
            result.valid,
            state.get("cypher_attempt_count", 0),
        )
        if result.valid:
            LOGGER.info(
                "Validated Cypher=%s parameter_names=%s",
                state["generated_cypher"],
                sorted(state.get("cypher_parameters", {})),
            )
        return {"cypher_validation_errors": list(result.errors)}

    def execute_neo4j(self, state: IncidentState) -> dict[str, Any]:
        if self._analytics_repository is None:
            return {
                "graph_results": [],
                "answer": "Graph analytics is temporarily unavailable.",
                "citations": [],
                "route": "answer_from_graph",
            }
        try:
            rows = self._analytics_repository.execute_read(
                state["generated_cypher"], state["cypher_parameters"]
            )
            LOGGER.info("Graph analytics returned rows=%d", len(rows))
            return {"graph_results": rows}
        except Exception:
            LOGGER.error("Neo4j graph analytics execution failed")
            return {
                "graph_results": [],
                "answer": "Graph analytics is temporarily unavailable.",
                "citations": [],
                "route": "answer_from_graph",
            }

    def answer_from_graph(self, state: IncidentState) -> dict[str, Any]:
        if state.get("answer"):
            return {}
        rows = state.get("graph_results", [])
        answerer = getattr(self._fh_genie, "answer_graph_question", None)
        answer = (
            answerer(state["normalized_query"], rows)
            if answerer is not None
            else self._format_graph_results(rows)
        )
        return {
            "answer": answer,
            "citations": [],
            "route": "answer_from_graph",
        }

    def graph_query_failed(self, state: IncidentState) -> dict[str, Any]:
        return {
            "answer": (
                "I could not produce a safe graph query after two attempts. "
                "Please make the analytics question more specific."
            ),
            "citations": [],
            "route": "answer_from_graph",
        }

    def retrieve_kubernetes_docs(self, state: IncidentState) -> dict[str, Any]:
        if self._documentation_retriever is None:
            return {"documentation_results": []}
        try:
            results = self._documentation_retriever.search(
                state["normalized_query"], limit=5
            )
            LOGGER.info("Documentation retrieval returned chunks=%d", len(results))
            return {"documentation_results": results}
        except Exception:
            LOGGER.exception("Documentation retrieval failed")
            return {"documentation_results": []}

    def answer_from_kubernetes_docs(self, state: IncidentState) -> dict[str, Any]:
        chunks = state.get("documentation_results", [])
        if not chunks:
            return {
                "answer": (
                    "I could not find relevant content in the indexed official "
                    "Kubernetes documentation."
                ),
                "citations": [],
                "route": "general_answer",
            }
        allowed_urls = {chunk["source_url"] for chunk in chunks}
        try:
            generated = self._fh_genie.answer_documentation_question(
                state["normalized_query"], chunks
            )
            if not set(generated.citations).issubset(allowed_urls):
                raise ValueError("documentation answer contained unsupported citation")
            answer = generated.answer
            citations = generated.citations
        except Exception:
            LOGGER.warning(
                "Documentation answer model unavailable; using extractive answer"
            )
            best = chunks[0]
            answer = self._extractive_documentation_answer(best)
            citations = [best["source_url"]]
        return {
            "answer": answer,
            "citations": citations,
            "route": "general_answer",
        }

    @staticmethod
    def _extractive_documentation_answer(chunk: dict[str, Any]) -> str:
        text = re.sub(r"^#{1,6}\s+.*$", "", chunk["text"], flags=re.MULTILINE)
        paragraphs = [item.strip() for item in re.split(r"\n{2,}", text) if item.strip()]
        excerpt = "\n\n".join(paragraphs[:2]).strip()
        if len(excerpt) > 900:
            excerpt = excerpt[:900].rsplit(" ", 1)[0] + "…"
        return excerpt or "The retrieved documentation did not contain explanatory text."

    def analyze_incident(self, state: IncidentState) -> dict[str, Any]:
        """Project the already validated semantic analysis into retrieval state."""

        understanding = state["query_understanding"]
        symptom = understanding.observed_behavior or next(
            iter(understanding.symptoms), None
        )
        analysis = QueryAnalysis(
            symptom=symptom,
            components=understanding.components,
            kubernetes_version=next(iter(understanding.versions), None),
            error_messages=understanding.error_messages,
            has_sufficient_details=understanding.has_concrete_incident,
            missing_details=state.get("missing_details", []),
        )
        return {
            "query_analysis": analysis,
            "extracted_symptom": analysis.symptom,
            "extracted_components": analysis.components,
            "kubernetes_version": analysis.kubernetes_version,
            "error_messages": analysis.error_messages,
            "has_sufficient_details": analysis.has_sufficient_details,
            "missing_details": analysis.missing_details,
        }

    def retrieve_evidence(self, state: IncidentState) -> dict[str, Any]:
        analysis = state["query_analysis"]
        context = self._retriever.search(
            query_text=state["user_query"],
            query_analysis=analysis,
            limit=5,
            debug=bool(state.get("debug")),
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
            route_reason = "verified actionable fix passed relevance thresholds"
        elif relevant:
            route = "generate_findings"
            route_reason = "relevant incidents found without a verified actionable fix"
        else:
            route = "request_clarification"
            if context.get("no_close_match"):
                route_reason = "no candidate passed deterministic reranking"
            elif score is None or score < self._settings.agent_relevance_threshold:
                route_reason = "top similarity score was below the evidence threshold"
            else:
                route_reason = "retrieved issues lacked meaningful diagnostic overlap"
        result: dict[str, Any] = {
            "actionable_fix_available": route == "generate_fix",
            "route": route,
            "route_reason": route_reason,
        }
        if route == "request_clarification":
            result["missing_details"] = self._clarification_details(
                state,
                after_no_match=bool(context.get("no_close_match")),
            )
        return result

    def generate_fix(self, state: IncidentState) -> dict[str, Any]:
        fixes = self._actionable_fixes(state.get("retrieved_context", {}))
        try:
            generated = self._fh_genie.generate_grounded_fix(
                state["user_query"],
                fixes,
            )
            answer = generated.answer
            verified_steps = generated.verified_steps
            citations = generated.citations
        except Exception:
            LOGGER.warning(
                "Grounded answer model unavailable; rendering verified evidence"
            )
            fallback = fixes[0]
            answer = fallback["resolution_summary"]
            verified_steps = fallback["verified_steps"]
            citations = fallback["evidence_urls"]
        steps = "\n".join(f"- {step}" for step in verified_steps)
        return {
            "answer": f"{answer}\nVerified steps:\n{steps}",
            "citations": citations,
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
        missing = self._clarification_details(
            state,
            after_no_match=bool(
                state.get("retrieved_context", {}).get("no_close_match")
            ),
        )
        prefix = (
            "No close historical incident was found. Please provide: "
            if state.get("retrieved_context", {}).get("no_close_match")
            else "Please provide: "
        )
        known: list[str] = []
        if state.get("error_messages"):
            known.append("error " + ", ".join(state["error_messages"]))
        if state.get("extracted_components"):
            known.append("component " + ", ".join(state["extracted_components"]))
        if state.get("kubernetes_version"):
            known.append("Kubernetes version " + state["kubernetes_version"])
        acknowledgement = (
            "I already have " + "; ".join(known) + ". " if known else ""
        )
        cross_version_notice = ""
        if state.get("retrieved_context", {}).get("cross_version_matches"):
            cross_version_notice = (
                "A similar symptom exists in another Kubernetes version, but its "
                "resolution must not be assumed to apply to your version. "
            )
        question = (
            acknowledgement
            + cross_version_notice
            + prefix
            + ", ".join(missing)
            + "."
        )
        attempts = state.get("clarification_attempts", 0)
        if self._enable_interrupts:
            if attempts >= 3:
                return {
                    "answer": (
                        "I could not collect enough diagnostic detail after three "
                        "clarification attempts. Still needed: "
                        + ", ".join(missing)
                        + "."
                    ),
                    "citations": [],
                    "route": "request_clarification",
                    "route_reason": "maximum clarification attempts reached",
                    "actionable_fix_available": False,
                    "clarification_exhausted": True,
                }
            response = interrupt(
                {
                    "question": question,
                    "known_details": self._known_details(state),
                    "missing_details": missing,
                }
            )
            return {
                "clarification_history": [
                    *state.get("clarification_history", []),
                    str(response).strip(),
                ],
                "clarification_attempts": attempts + 1,
                "clarification_exhausted": False,
                "answer": "",
                "citations": [],
            }
        return {
            "answer": question,
            "citations": [],
            "route": "request_clarification",
            "route_reason": state.get(
                "route_reason",
                "incident description lacks sufficient diagnostic detail",
            ),
            "actionable_fix_available": False,
        }

    @staticmethod
    def _known_details(state: IncidentState) -> dict[str, Any]:
        known: dict[str, Any] = {}
        if state.get("extracted_symptom"):
            known["symptom"] = state["extracted_symptom"]
        if state.get("extracted_components"):
            known["components"] = state["extracted_components"]
        if state.get("kubernetes_version"):
            known["kubernetes_version"] = state["kubernetes_version"]
        if state.get("error_messages"):
            known["error_messages"] = state["error_messages"]
        return known

    def _clarification_details(
        self,
        state: IncidentState,
        *,
        after_no_match: bool,
    ) -> list[str]:
        missing = list(state.get("missing_details", []))
        if state.get("error_messages"):
            missing = [item for item in missing if "error message" not in item]
        if state.get("extracted_components"):
            missing = [item for item in missing if "component" not in item]
        if state.get("kubernetes_version"):
            missing = [item for item in missing if "version" not in item.lower()]
        if after_no_match:
            missing = [item for item in missing if item != "relevant logs"]
            missing = self._unique(
                [
                    *missing,
                    "relevant logs around the failure",
                    "pod events",
                    "container exit reason",
                    "whether one pod or multiple pods are affected",
                    "what changed immediately before the problem",
                ]
            )
        if not missing:
            missing = ["specific observed behaviour and relevant logs"]
        return self._unique(missing)

    def _has_evidence_overlap(
        self,
        state: IncidentState,
        matched: list[dict[str, Any]],
    ) -> bool:
        """Require compatibility between model-extracted diagnostic fields."""

        understanding = state["query_understanding"]
        query_components = {item.casefold() for item in understanding.components}
        query_versions = {item.casefold() for item in understanding.versions}
        query_diagnostics = {
            item.casefold()
            for item in [
                *understanding.symptoms,
                *understanding.error_messages,
                understanding.observed_behavior,
            ]
            if item
        }
        for issue in matched:
            issue_components = {
                str(item).casefold() for item in issue.get("components", [])
            }
            issue_versions = {
                str(item).casefold() for item in issue.get("kubernetes_versions", [])
            }
            issue_diagnostics = {
                str(item).casefold()
                for item in [
                    issue.get("symptom"),
                    *issue.get("error_messages", []),
                ]
                if item
            }
            component_match = bool(query_components & issue_components)
            version_match = bool(query_versions & issue_versions)
            diagnostic_match = any(
                query_value in issue_value or issue_value in query_value
                for query_value in query_diagnostics
                for issue_value in issue_diagnostics
            )
            if query_diagnostics:
                if diagnostic_match and (component_match or version_match):
                    return True
            elif component_match or version_match:
                return True
        return False

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

    @staticmethod
    def _preserves_technical_tokens(original: str, normalized: str) -> bool:
        tokens = re.findall(
            r"`[^`]+`|\bCVE-\d{4}-\d+\b|\bv?\d+\.\d+(?:\.\d+)?\b|"
            r"\bCrashLoopBackOff\b|\b[A-Za-z][A-Za-z0-9]+Error\b|#\d+",
            original,
            re.I,
        )
        return all(token.lower() in normalized.lower() for token in tokens)

    @classmethod
    def _normalization_from_state(cls, state: IncidentState) -> NormalizedQuery:
        version = state.get("kubernetes_version")
        return NormalizedQuery(
            normalized_query=state["normalized_query"],
            components=state.get("extracted_components", []),
            versions=[version] if version else [],
            symptoms=[],
            entities=state.get("entities", []),
        )

    @staticmethod
    def _format_graph_results(rows: list[dict[str, Any]]) -> str:
        if not rows:
            return "The knowledge graph returned no matching results."
        return "Graph results:\n" + "\n".join(
            "- " + ", ".join(f"{key}: {value}" for key, value in row.items())
            for row in rows
        )

    @staticmethod
    def _analytics_cypher(
        understanding: QueryUnderstanding,
    ) -> CypherGeneration | None:
        operation = understanding.analytics_operation
        group_by = understanding.group_by
        limit = understanding.limit or 20
        if operation == "TOP_N" and group_by == "component":
            return CypherGeneration(
                cypher=(
                    "MATCH (i:Issue)-[:AFFECTS_COMPONENT]->(c:Component) "
                    "RETURN c.name AS component, count(DISTINCT i) AS issue_count "
                    f"ORDER BY issue_count DESC LIMIT {limit}"
                ),
                parameters={},
                explanation="Rank components by distinct issue count",
            )
        if operation in {"COUNT", "GROUP_BY"} and group_by == "version":
            return CypherGeneration(
                cypher=(
                    "MATCH (i:Issue)-[:REPORTED_IN_VERSION]->(v:KubernetesVersion) "
                    "RETURN v.version AS kubernetes_version, "
                    "count(DISTINCT i) AS incident_count "
                    f"ORDER BY incident_count DESC LIMIT {limit}"
                ),
                parameters={},
                explanation="Count incidents by Kubernetes version",
            )
        if operation == "RELATIONSHIP" and len(understanding.components) >= 2:
            components = list(dict.fromkeys(
                component.lower() for component in understanding.components
            ))
            return CypherGeneration(
                cypher=(
                    "MATCH (i:Issue)-[:AFFECTS_COMPONENT]->(c:Component) "
                    "WHERE toLower(c.name) IN $components "
                    "WITH i, count(DISTINCT toLower(c.name)) AS matched_components "
                    "WHERE matched_components = $component_count "
                    "RETURN i.issue_number AS issue_number, i.title AS title, "
                    f"i.html_url AS url ORDER BY issue_number DESC LIMIT {limit}"
                ),
                parameters={
                    "components": components,
                    "component_count": len(components),
                },
                explanation="Find issues connected to every requested component",
            )
        if operation == "LIST" and group_by == "issue" and understanding.components:
            parameters: dict[str, Any] = {
                "component": understanding.components[0].lower(),
            }
            version_match = ""
            version_filter = ""
            if understanding.versions:
                version_match = (
                    "MATCH (i)-[:REPORTED_IN_VERSION]->(v:KubernetesVersion) "
                )
                version_filter = " AND v.version = $version"
                parameters["version"] = understanding.versions[0]
            return CypherGeneration(
                cypher=(
                    "MATCH (i:Issue)-[:AFFECTS_COMPONENT]->(c:Component) "
                    + version_match
                    + "WHERE toLower(c.name) = $component"
                    + version_filter
                    + " RETURN i.issue_number AS issue_number, i.title AS title, "
                    "i.summary AS summary, i.html_url AS url, "
                    "coalesce(i.comments_count, 0) AS discussion_count "
                    f"ORDER BY discussion_count DESC LIMIT {limit}"
                ),
                parameters=parameters,
                explanation="List issues matching structured component filters",
            )
        if operation in {"COUNT", "GROUP_BY"} and group_by == "component":
            return CypherGeneration(
                cypher=(
                    "MATCH (i:Issue)-[:AFFECTS_COMPONENT]->(c:Component) "
                    "RETURN c.name AS component, count(DISTINCT i) AS issue_count "
                    f"ORDER BY issue_count DESC LIMIT {limit}"
                ),
                parameters={},
                explanation="Count incidents by component",
            )
        return None
