import logging
import re
from typing import Any

from langgraph.types import interrupt
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agent.state import (
    CypherGeneration,
    IncidentState,
    IntentClassification,
    NormalizedQuery,
    QueryAnalysis,
    Route,
)
from app.enrichment.fh_genie_client import FHGenieClient
from app.enrichment.models import IssueEnrichment
from app.graph.analytics import GraphAnalyticsRepository
from app.graph.cypher_validation import CypherValidator
from app.retrieval.graph_retriever import GraphRetriever


LOGGER = logging.getLogger(__name__)


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
        *,
        analytics_repository: GraphAnalyticsRepository | None = None,
        cypher_validator: CypherValidator | None = None,
        enable_interrupts: bool = False,
    ) -> None:
        self._fh_genie = fh_genie
        self._retriever = retriever
        self._settings = settings
        self._analytics_repository = analytics_repository
        self._cypher_validator = cypher_validator or CypherValidator()
        self._enable_interrupts = enable_interrupts

    def normalize_query(self, state: IncidentState) -> dict[str, Any]:
        original_query = state.get("original_query") or state["user_query"]
        history = list(state.get("clarification_history", []))
        query = "\n".join(
            [original_query]
            + [
                f"Clarification {index}: {answer}"
                for index, answer in enumerate(history, 1)
            ]
        )
        normalized = self._deterministic_normalization(query)
        model_normalizer = getattr(self._fh_genie, "normalize_query", None)
        if model_normalizer is not None:
            try:
                candidate = model_normalizer(query)
                if self._preserves_technical_tokens(query, candidate.normalized_query):
                    normalized = candidate
            except Exception:
                LOGGER.warning("Query normalization model output was invalid")
        return {
            "original_query": original_query,
            "user_query": normalized.normalized_query,
            "normalized_query": normalized.normalized_query,
            "normalized_symptoms": normalized.symptoms,
            "extracted_components": normalized.components,
            "kubernetes_version": normalized.versions[0] if normalized.versions else None,
            "entities": normalized.entities,
            "error_messages": [],
            "has_sufficient_details": False,
            "missing_details": [
                "specific observed behaviour",
                "exact error message or error code",
                "affected component",
                "Kubernetes version",
                "relevant logs",
            ],
            "cypher_attempt_count": 0,
            "cypher_validation_errors": [],
        }

    def classify_intent(self, state: IncidentState) -> dict[str, Any]:
        query = state["normalized_query"]
        history = state.get("clarification_history", [])
        routing_query = history[-1] if history else query
        decision = self._deterministic_intent(routing_query)
        if decision is None:
            classifier = getattr(self._fh_genie, "classify_query", None)
            if classifier is not None:
                try:
                    decision = classifier(self._normalization_from_state(state))
                except Exception:
                    LOGGER.warning("Intent classifier returned invalid output")
        if decision is None:
            decision = IntentClassification(
                intent="INCIDENT_TROUBLESHOOTING",
                confidence=0.55,
                reasoning="Conservative fallback preserves the existing incident flow",
            )
        LOGGER.info(
            "Query routed intent=%s confidence=%.2f",
            decision.intent,
            decision.confidence,
        )
        return {
            "intent": decision.intent,
            "intent_confidence": decision.confidence,
            "intent_reasoning": decision.reasoning,
        }

    def generate_cypher(self, state: IncidentState) -> dict[str, Any]:
        attempt = state.get("cypher_attempt_count", 0) + 1
        try:
            normalized = self._normalization_from_state(state)
            generated = self._deterministic_cypher(normalized)
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

    @staticmethod
    def answer_general_question(state: IncidentState) -> dict[str, Any]:
        return {
            "answer": (
                "General Kubernetes documentation retrieval is not configured yet. "
                "I will not infer a conceptual answer from incident records."
            ),
            "citations": [],
            "route": "general_answer",
        }

    def analyze_incident(self, state: IncidentState) -> dict[str, Any]:
        original_query = state.get("original_query") or state["user_query"]
        normalized_query = state.get("normalized_query") or original_query
        history = list(state.get("clarification_history", []))
        analysis_query = normalized_query
        try:
            facts = self._fh_genie.enrich(analysis_query, None)
        except Exception:
            LOGGER.warning(
                "Incident analysis model unavailable; using deterministic extraction"
            )
            facts = IssueEnrichment(
                symptom=(state.get("normalized_symptoms") or [None])[0],
                components=state.get("extracted_components", []),
                kubernetes_versions=(
                    [state["kubernetes_version"]]
                    if state.get("kubernetes_version")
                    else []
                ),
                error_messages=[],
            )
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
            analysis_query,
        )
        error_messages = self._unique(
            [
                *[
                    message
                    for message in error_messages
                    if self._text_is_present(message, analysis_query)
                ],
                *query_error_signals,
            ]
        )
        version = next(
            (
                item
                for item in facts.kubernetes_versions
                if self._text_is_present(item, analysis_query)
            ),
            None,
        )
        behavior = self._has_problematic_behavior(analysis_query)
        previous = state.get("query_analysis")
        components = self._unique(
            [
                *(previous.components if previous else []),
                *[
                    component
                    for component in facts.components
                    if self._component_is_present(component, analysis_query)
                ],
            ]
        )
        error_messages = self._unique(
            [*(previous.error_messages if previous else []), *error_messages]
        )
        symptom = (
            symptom
            if specific_symptom
            else (previous.symptom if previous else None)
        )
        specific_symptom = bool(symptom and not self._is_generic(symptom))
        version = version or (previous.kubernetes_version if previous else None)
        meaningful_symptom = bool(
            error_messages
            or (
                specific_symptom
                and not self._is_generic_incident_query(analysis_query)
                and behavior
            )
        )
        has_diagnostic_detail = bool(
            components
            or version
            or self._resource_kinds(analysis_query)
            or self._has_log_message(analysis_query)
            or self._error_codes(analysis_query)
        )
        has_sufficient_details = meaningful_symptom and has_diagnostic_detail
        missing_details: list[str] = []
        if not meaningful_symptom:
            missing_details.append("specific observed behaviour")
        if not error_messages:
            missing_details.append("exact error message or error code")
        if not components:
            missing_details.append("affected component")
        if not version:
            missing_details.append("Kubernetes version")
        if not re.search(r"\b(log|logs|stack trace|traceback)\b", state["user_query"], re.I):
            missing_details.append("relevant logs")
        analysis = QueryAnalysis(
            symptom=symptom if specific_symptom else None,
            components=components,
            kubernetes_version=version,
            error_messages=error_messages,
            has_sufficient_details=has_sufficient_details,
            missing_details=missing_details,
        )
        return {
            "original_query": original_query,
            "user_query": analysis_query,
            "clarification_history": history,
            "clarification_attempts": state.get("clarification_attempts", 0),
            "clarification_exhausted": False,
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
            query = state.get("user_query", "")
            missing = [item for item in missing if item != "relevant logs"]
            log_request = (
                "relevant logs around the restart"
                if re.search(r"\brestart(?:s|ed|ing)?\b", query, re.I)
                else "relevant logs around the failure"
            )
            missing = self._unique(
                [
                    *missing,
                    log_request,
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
    def _is_generic_incident_query(cls, value: str) -> bool:
        normalized = " ".join(re.findall(r"[a-z0-9]+", value.lower()))
        contains_generic_phrase = any(
            re.search(pattern, normalized)
            for pattern in cls.GENERIC_QUERY_PATTERNS
        )
        if not contains_generic_phrase:
            return False
        # An earlier vague turn must not veto a later clarification that adds
        # concrete observed behaviour.
        return not bool(set(normalized.split()) & cls.BEHAVIOR_TERMS)

    @staticmethod
    def _text_is_present(value: str, query: str) -> bool:
        normalized_value = " ".join(re.findall(r"[a-z0-9]+", value.lower()))
        normalized_query = " ".join(re.findall(r"[a-z0-9]+", query.lower()))
        return bool(normalized_value and normalized_value in normalized_query)

    @classmethod
    def _component_is_present(cls, component: str, query: str) -> bool:
        normalized = " ".join(re.findall(r"[a-z0-9]+", component.lower()))
        if normalized in cls.GENERIC_COMPONENTS:
            return False
        return cls._text_is_present(component, query)

    @classmethod
    def _resource_kinds(cls, query: str) -> set[str]:
        tokens = set(re.findall(r"[a-z0-9]+", query.lower()))
        return tokens & cls.RESOURCE_KINDS

    @staticmethod
    def _has_log_message(query: str) -> bool:
        return bool(
            re.search(
                r"(?:\blog(?:s| message)?\s*[:=]\s*\S|`[^`]+`|\"[^\"]+\")",
                query,
                re.I,
            )
        )

    @staticmethod
    def _error_codes(query: str) -> list[str]:
        return re.findall(
            r"\b(?:[45]\d\d|0x[0-9a-fA-F]+|[A-Z][A-Z0-9_-]*\d+[A-Z0-9_-]*)\b",
            query,
        )

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

    @classmethod
    def _deterministic_normalization(cls, query: str) -> NormalizedQuery:
        cleaned = " ".join(query.strip().split())
        versions = cls._unique(
            re.findall(r"\bv?\d+\.\d+(?:\.\d+)?\b", cleaned, re.I)
        )
        component_terms = (
            "kubelet",
            "kube-apiserver",
            "api server",
            "scheduler",
            "controller manager",
            "etcd",
            "containerd",
            "kube-proxy",
            "certificate",
            "certificates",
            "pod",
            "pods",
        )
        components = [
            term for term in component_terms if re.search(rf"\b{re.escape(term)}\b", cleaned, re.I)
        ]
        entities = cls._unique(
            [
                *components,
                *versions,
                *re.findall(r"\bCVE-\d{4}-\d+\b", cleaned, re.I),
                *re.findall(r"(?:#|issues?/)(\d+)\b", cleaned, re.I),
            ]
        )
        symptoms = (
            [cleaned]
            if cls._has_problematic_behavior(cleaned)
            or re.search(r"CrashLoopBackOff|crash(?:es|ed|ing)?", cleaned, re.I)
            else []
        )
        return NormalizedQuery(
            normalized_query=cleaned,
            components=components,
            versions=versions,
            symptoms=symptoms,
            entities=entities,
        )

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

    @classmethod
    def _deterministic_intent(
        cls, query: str
    ) -> IntentClassification | None:
        normalized = " ".join(query.lower().split())
        if normalized in cls.GENERIC_DESCRIPTIONS or normalized in {
            "help",
            "it failed",
        }:
            return IntentClassification(
                intent="NEEDS_CLARIFICATION",
                confidence=0.99,
                reasoning="The query does not identify a concrete question or symptom",
            )
        if re.search(
            r"\b(most common|how many|count|top\s+\d*|which components?|"
            r"associated with|affect both|most reported|group by|trend)\b",
            normalized,
        ):
            return IntentClassification(
                intent="GRAPH_ANALYTICS",
                confidence=0.98,
                reasoning="The query requests aggregation or graph relationships",
            )
        if re.match(r"^(what does|what is|how does|explain|describe)\b", normalized):
            return IntentClassification(
                intent="GENERAL_KUBERNETES_QUESTION",
                confidence=0.97,
                reasoning="The query asks for conceptual Kubernetes knowledge",
            )
        if re.match(r"^why do(?:es)?\b", normalized) and not re.search(
            r"\b(my|our|after|started|suddenly)\b", normalized
        ):
            return IntentClassification(
                intent="GENERAL_KUBERNETES_QUESTION",
                confidence=0.9,
                reasoning="The query asks for a general mechanism",
            )
        if re.search(
            r"\b(my|our|started|after|upgrade|upgrading|crash(?:es|ed|ing)?|"
            r"failing|failed|error|stuck|pending|restart(?:s|ed|ing)?)\b",
            normalized,
        ):
            return IntentClassification(
                intent="INCIDENT_TROUBLESHOOTING",
                confidence=0.95,
                reasoning="The query describes a concrete observed failure",
            )
        return None

    @staticmethod
    def _format_graph_results(rows: list[dict[str, Any]]) -> str:
        if not rows:
            return "The knowledge graph returned no matching results."
        return "Graph results:\n" + "\n".join(
            "- " + ", ".join(f"{key}: {value}" for key, value in row.items())
            for row in rows
        )

    @staticmethod
    def _deterministic_cypher(
        normalized: NormalizedQuery,
    ) -> CypherGeneration | None:
        query = normalized.normalized_query.lower()
        components = [
            component.lower()
            for component in normalized.components
            if component.lower() not in {"pod", "pods"}
        ]
        versions = normalized.versions

        if re.search(r"\b(which components?|most reported issues)\b", query):
            return CypherGeneration(
                cypher=(
                    "MATCH (i:Issue)-[:AFFECTS_COMPONENT]->(c:Component) "
                    "RETURN c.name AS component, count(DISTINCT i) AS issue_count "
                    "ORDER BY issue_count DESC LIMIT 20"
                ),
                parameters={},
                explanation="Count reported issues by component",
            )

        if "affect both" in query and len(components) >= 2:
            selected = list(dict.fromkeys(components))
            return CypherGeneration(
                cypher=(
                    "MATCH (i:Issue)-[:AFFECTS_COMPONENT]->(c:Component) "
                    "WHERE toLower(c.name) IN $components "
                    "WITH i, count(DISTINCT toLower(c.name)) AS matched_components "
                    "WHERE matched_components = $component_count "
                    "RETURN i.issue_number AS issue_number, i.title AS title, "
                    "i.html_url AS url ORDER BY issue_number DESC LIMIT 20"
                ),
                parameters={
                    "components": selected,
                    "component_count": len(selected),
                },
                explanation="Find issues linked to every requested component",
            )

        if re.search(r"\b(associated with|certificate failures?)\b", query):
            term = "certificate" if "certificate" in query else "failure"
            return CypherGeneration(
                cypher=(
                    "MATCH (i:Issue)-[:AFFECTS_COMPONENT]->(c:Component) "
                    "OPTIONAL MATCH (i)-[:HAS_ERROR]->(e:ErrorMessage) "
                    "WHERE toLower(coalesce(i.symptom, '')) CONTAINS $term "
                    "OR toLower(coalesce(i.summary, '')) CONTAINS $term "
                    "OR toLower(coalesce(e.message, '')) CONTAINS $term "
                    "RETURN c.name AS component, count(DISTINCT i) AS issue_count "
                    "ORDER BY issue_count DESC LIMIT 20"
                ),
                parameters={"term": term},
                explanation="Count components linked to matching incident text",
            )

        if "most common" in query and components:
            filters = ["toLower(c.name) = $component"]
            parameters: dict[str, Any] = {"component": components[0]}
            version_match = ""
            if versions:
                version_match = (
                    "MATCH (i)-[:REPORTED_IN_VERSION]->(v:KubernetesVersion) "
                )
                filters.append("v.version = $version")
                parameters["version"] = versions[0]
            return CypherGeneration(
                cypher=(
                    "MATCH (i:Issue)-[:AFFECTS_COMPONENT]->(c:Component) "
                    + version_match
                    + "WHERE "
                    + " AND ".join(filters)
                    + " RETURN i.issue_number AS issue_number, i.title AS title, "
                    "i.summary AS summary, i.html_url AS url, "
                    "coalesce(i.comments_count, 0) AS discussion_count "
                    "ORDER BY discussion_count DESC LIMIT 20"
                ),
                parameters=parameters,
                explanation="List the most discussed matching issues",
            )
        return None

    GENERIC_DESCRIPTIONS = {
        "not working",
        "broken",
        "has an issue",
        "there is an error",
        "something is wrong",
        "kubernetes problem",
        "kubernetes is not working",
        "something is broken",
        "behave incorrectly",
        "something failed",
    }
    GENERIC_QUERY_PATTERNS = (
        r"\bnot working\b",
        r"\bbehav(?:e|es|ed|ing) incorrectly\b",
        r"\bhas an issue\b",
        r"\bsomething fail(?:s|ed|ing)?\b",
        r"\bkubernetes (?:object|objects|problem)\b",
    )
    GENERIC_COMPONENTS = {"kubernetes", "object", "objects", "cluster"}
    RESOURCE_KINDS = {
        "configmap",
        "configmaps",
        "cronjob",
        "cronjobs",
        "daemonset",
        "daemonsets",
        "deployment",
        "deployments",
        "job",
        "jobs",
        "namespace",
        "namespaces",
        "node",
        "nodes",
        "pod",
        "pods",
        "secret",
        "secrets",
        "service",
        "services",
        "statefulset",
        "statefulsets",
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
        "process",
        "processes",
        "processing",
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
