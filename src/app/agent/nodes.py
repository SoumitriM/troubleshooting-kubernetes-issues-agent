import logging
import re
from pathlib import Path
from typing import Any

from langgraph.types import interrupt
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agent.state import (
    ComposedReply,
    CypherGeneration,
    IncidentState,
    NormalizedQuery,
    QueryAnalysis,
    QueryUnderstanding,
    Route,
)
from app.enrichment.claude_client import ClaudeClient
from app.documentation.store import KubernetesDocsRetriever
from app.graph.analytics import GraphAnalyticsRepository
from app.graph.cypher_validation import CypherValidator
from app.graph.formatting import format_graph_answer, query_limit
from app.retrieval.graph_retriever import GraphRetriever


LOGGER = logging.getLogger(__name__)


# Component names are stored as reported; the query understanding may name the
# same component with or without its "kube-" binary prefix.
COMPONENT_KEY = "replace(toLower(c.name), 'kube-', '')"

# Read-only commands the clarification reply may ask the user to run. The
# composer may quote only commands that appear in its payload.
DIAGNOSTIC_COMMANDS = [
    "kubectl get pods -A",
    "kubectl describe pod <pod-name>",
    "kubectl logs <pod-name> --previous",
    "kubectl get events --sort-by=.lastTimestamp",
    "kubectl get nodes",
    "kubectl version",
    "journalctl -u kubelet",
]
# Facts sent to the composer are trimmed so one reply stays a cheap call.
COMPOSE_MAX_ROWS = 25
COMPOSE_TEXT_CHARS = 600


class AgentSettings(BaseSettings):
    """Deterministic routing thresholds loaded from environment or .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    agent_relevance_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    agent_actionable_fix_threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    documentation_index_path: Path = Path("data/kubernetes_docs.sqlite")


OUT_OF_SCOPE_ANSWER = (
    "I can only help with Kubernetes: troubleshooting incidents, "
    "questions about reported issues in the knowledge graph, and "
    "how Kubernetes works. I answer from read-only evidence and "
    "cannot change data or run commands. Please ask a "
    "Kubernetes question."
)


class IncidentNodes:
    """LangGraph node implementations with injected external dependencies."""

    def __init__(
        self,
        llm: ClaudeClient,
        retriever: GraphRetriever,
        settings: AgentSettings,
        *,
        analytics_repository: GraphAnalyticsRepository | None = None,
        documentation_retriever: KubernetesDocsRetriever | None = None,
        cypher_validator: CypherValidator | None = None,
        enable_interrupts: bool = False,
    ) -> None:
        self._llm = llm
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
        understanding_unavailable = False
        try:
            history = state.get("conversation_history") or []
            analysis = (
                self._llm.analyze_query(query, history=history)
                if history
                else self._llm.analyze_query(query)
            )
            if not self._preserves_technical_tokens(query, analysis.normalized_query):
                # The structured interpretation is still usable; only the rewritten
                # text is untrusted, so retrieval falls back to the user's wording.
                LOGGER.warning("Normalized query lost a technical token; using original")
                analysis = analysis.model_copy(update={"normalized_query": query.strip()})
        except Exception:
            LOGGER.exception("Claude query understanding unavailable")
            understanding_unavailable = True
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
            "query_understanding_unavailable": understanding_unavailable,
            "out_of_scope": not analysis.in_scope,
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
            "suggestions": [],
            "clarification_question": "",
        }

    def _converse(
        self, state: IncidentState, situation: str, facts: dict[str, Any]
    ) -> ComposedReply | None:
        """Ask the model for a conversational reply; ``None`` keeps the template.

        Routes build their deterministic answer first, so a failed or missing
        composer only costs tone, never content.
        """

        composer = getattr(self._llm, "compose_reply", None)
        if composer is None or state.get("query_understanding_unavailable"):
            return None
        original = state.get("original_query") or state.get("user_query", "")
        user_message = "\n".join(
            [original, *state.get("clarification_history", [])]
        )
        try:
            composed = composer(
                situation,
                user_message=user_message,
                facts=facts,
                history=state.get("conversation_history") or None,
            )
        except Exception:
            LOGGER.warning("Reply composer unavailable for situation=%s", situation)
            return None
        return composed if isinstance(composed, ComposedReply) else None

    @staticmethod
    def _issue_facts(issue: dict[str, Any]) -> dict[str, Any]:
        return {
            key: (
                value[:COMPOSE_TEXT_CHARS] if isinstance(value, str) else value
            )
            for key, value in {
                "issue_number": issue.get("issue_number"),
                "title": issue.get("title"),
                "summary": issue.get("summary"),
                "symptom": issue.get("symptom"),
                "components": (issue.get("components") or [])[:6],
                "kubernetes_versions": (issue.get("kubernetes_versions") or [])[:4],
                "state": issue.get("state"),
            }.items()
            if value
        }

    def generate_cypher(self, state: IncidentState) -> dict[str, Any]:
        attempt = state.get("cypher_attempt_count", 0) + 1
        try:
            normalized = self._normalization_from_state(state)
            alternative = bool(state.get("alternative_attempted"))
            generated = (
                None if alternative else self._analytics_cypher(state["query_understanding"])
            )
            source = "deterministic"
            if generated is None:
                source = "model"
                hints = state.get("alternative_hints") or []
                generated = self._llm.generate_cypher(
                    normalized,
                    intent=state["intent"],
                    validation_errors=state.get("cypher_validation_errors", []),
                    **({"hints": hints} if hints else {}),
                )
            LOGGER.info(
                "Generated Cypher attempt=%d source=%s",
                attempt,
                source,
            )
            deterministic = source == "deterministic"
            return {
                "generated_cypher": generated.cypher,
                "cypher_parameters": generated.parameters,
                "cypher_explanation": generated.explanation,
                "cypher_attempt_count": attempt,
                "graph_display_limit": (
                    self._display_limit(state["query_understanding"])
                    if deterministic
                    else query_limit(generated.cypher)
                ),
                "graph_limit_probed": deterministic,
                "cypher_source": source,
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

    def prepare_alternative(self, state: IncidentState) -> dict[str, Any]:
        """Collect graph names close to the question's terms for one broader query.

        A template found no rows, usually because the question names a
        component or version differently from the graph ("DRA admin access"
        vs "draadminaccess"). The hints are real graph values, so Claude's
        alternative query is grounded in what exists.
        """

        understanding = state["query_understanding"]
        tokens = sorted({
            token
            for value in [*understanding.components, *understanding.entities]
            for token in re.findall(r"[a-z0-9]+", value.lower())
            if len(token) > 2 and token not in {"kube", "kubernetes", "issue", "issues"}
        })
        hints: list[str] = []
        if tokens and self._analytics_repository is not None:
            try:
                rows = self._analytics_repository.execute_read(
                    "MATCH (c:Component) WHERE any(token IN $tokens WHERE "
                    "replace(replace(toLower(c.name), '-', ''), ' ', '') CONTAINS token) "
                    "RETURN c.name AS name ORDER BY size(c.name) LIMIT 15",
                    {"tokens": tokens},
                )
                names = [row["name"] for row in rows]
                if names:
                    hints.append("Component names in the graph similar to the question: " + ", ".join(names))
            except Exception:
                LOGGER.warning("Could not collect alternative query hints")
        if understanding.versions:
            hints.append(
                "Versions are stored as reported, e.g. 1.36, v1.36.2, v1.36.x; "
                "match the minor version with a regular expression."
            )
        hints.append("Original structured query (returned no rows): " + state.get("generated_cypher", ""))
        LOGGER.info("Trying a broader alternative query hints=%d", len(hints))
        return {
            "alternative_attempted": True,
            "alternative_hints": hints,
            "cypher_attempt_count": 0,
            "cypher_validation_errors": [],
        }

    def answer_from_graph(self, state: IncidentState) -> dict[str, Any]:
        if state.get("answer"):
            return {}
        rows = state.get("graph_results", [])
        answerer = getattr(self._llm, "answer_graph_question", None)
        if answerer is not None:
            answer, citations = answerer(state["normalized_query"], rows), []
        else:
            answer, citations = format_graph_answer(
                rows,
                display_limit=state.get("graph_display_limit"),
                probed=bool(state.get("graph_limit_probed")),
            )
            broadened = bool(state.get("alternative_attempted"))
            if broadened:
                answer = (
                    "No reported issues matched the exact names in your question"
                    + (", or a broader search." if not rows else ". A broader search found:\n")
                    + (answer if rows else "")
                )
            composed = self._converse(
                state,
                "analytics" if rows else "analytics_empty",
                {
                    "question": state["normalized_query"],
                    "rows": rows[:COMPOSE_MAX_ROWS],
                    "total_rows": len(rows),
                    "shown_below": answer if rows else None,
                    "broadened": broadened,
                },
            )
            if composed is not None:
                answer = composed.reply + ("\n\n" + answer if rows else "")
                return {
                    "answer": answer,
                    "citations": self._unique([*citations, *composed.citations]),
                    "suggestions": composed.suggestions,
                    "route": "answer_from_graph",
                }
        return {
            "answer": answer,
            "citations": citations,
            "suggestions": [],
            "route": "answer_from_graph",
        }

    def graph_query_failed(self, state: IncidentState) -> dict[str, Any]:
        if state.get("alternative_attempted"):
            # The structured query already ran and found nothing; only the
            # broader alternative failed validation.
            situation = "analytics_empty"
            answer = "The knowledge graph returned no matching results."
        else:
            situation = "graph_failed"
            answer = (
                "I could not produce a safe graph query after two attempts. "
                "Please make the analytics question more specific."
            )
        composed = self._converse(
            state, situation, {"question": state.get("normalized_query")}
        )
        return {
            "answer": composed.reply if composed else answer,
            "citations": [],
            "suggestions": composed.suggestions if composed else [],
            "route": "answer_from_graph",
        }

    MAX_RESEARCH_TOOL_CALLS = 4

    def research(self, state: IncidentState) -> dict[str, Any]:
        """Answer a multi-source question with a bounded, read-only tool loop.

        Claude chooses each tool call and when to stop. Tools reuse the same
        retrieval, template analytics and documentation search as the single
        routes, and the final answer may cite only URLs the tools returned.
        Any failure falls back to the question's primary route.
        """

        stepper = getattr(self._llm, "research_step", None)
        if stepper is None:
            return {"research_failed": True}
        question = state["normalized_query"]
        observations: list[dict[str, Any]] = []
        allowed_urls: set[str] = set()
        steps: list[dict[str, Any]] = []
        try:
            for _ in range(self.MAX_RESEARCH_TOOL_CALLS + 1):
                remaining = self.MAX_RESEARCH_TOOL_CALLS - len(observations)
                step = stepper(
                    question,
                    observations,
                    remaining_tool_calls=remaining,
                    allowed_urls=allowed_urls,
                )
                steps.append(step.model_dump(include={"action", "reason", "query", "operation", "components"}))
                if step.action == "final_answer":
                    LOGGER.info("Research answered after tool_calls=%d", len(observations))
                    return {
                        "answer": step.answer,
                        "citations": list(dict.fromkeys(step.citations)),
                        "suggestions": list(getattr(step, "suggestions", [])),
                        "route": "research_answer",
                        "research_steps": steps,
                    }
                result, urls = self._research_tool(state, step)
                allowed_urls.update(urls)
                observations.append({"tool": step.action, "input": steps[-1], "result": result})
        except Exception:
            LOGGER.exception("Research loop failed; using the primary route")
        return {"research_failed": True, "research_steps": steps}

    def _research_tool(self, state: IncidentState, step: Any) -> tuple[Any, set[str]]:
        understanding = state["query_understanding"]
        if step.action == "search_docs":
            if self._documentation_retriever is None:
                return "Documentation search is unavailable.", set()
            chunks = self._documentation_retriever.search(step.query, limit=4)
            return (
                [{"url": c["source_url"], "section": c["section"], "text": c["text"][:1500]} for c in chunks],
                {c["source_url"] for c in chunks},
            )
        if step.action == "search_incidents":
            analysis = QueryAnalysis(
                symptom=step.query,
                components=step.components or understanding.components,
                kubernetes_version=next(iter(step.versions or understanding.versions), None),
                error_messages=understanding.error_messages,
                has_sufficient_details=True,
            )
            context = self._retriever.search(
                query_text=step.query, query_analysis=analysis, limit=3, debug=False
            )
            issues = []
            urls: set[str] = set()
            for issue in context.get("matched_issues", [])[:3]:
                record = {
                    "issue_number": issue.get("issue_number"),
                    "title": issue.get("title"),
                    "summary": issue.get("summary"),
                    "url": issue.get("url"),
                }
                # Same rules as generate_fix: only verified, actionable resolutions.
                fix = next(iter(self._actionable_fixes({"matched_issues": [issue]})), None)
                if fix:
                    record["verified_fix"] = {
                        "summary": fix.get("resolution_summary"),
                        "steps": fix.get("verified_steps", []),
                        "evidence_urls": fix.get("evidence_urls", []),
                    }
                    urls.update(fix.get("evidence_urls", []))
                urls.update(self._finding_urls(issue))
                issues.append(record)
            return (issues or "No close historical incident was found."), urls
        if step.action == "graph_analytics":
            if self._analytics_repository is None:
                return "Graph analytics is unavailable.", set()
            request = QueryUnderstanding(
                normalized_query=state["normalized_query"],
                intent="GRAPH_ANALYTICS",
                intent_confidence=1.0,
                components=step.components,
                versions=step.versions,
                analytics_operation=step.operation,
                group_by=step.group_by,
                reasoning="research tool call",
            )
            generated = self._analytics_cypher(request)
            if generated is None:
                return "No analytics template fits that operation; try another grouping.", set()
            if not self._cypher_validator.validate(generated.cypher, generated.parameters).valid:
                return "The analytics query failed validation.", set()
            rows = self._analytics_repository.execute_read(generated.cypher, generated.parameters)
            answer, citations = format_graph_answer(
                rows, display_limit=self._display_limit(request), probed=True
            )
            return answer, set(citations)
        return "Unknown tool.", set()

    def retrieve_kubernetes_docs(self, state: IncidentState) -> dict[str, Any]:
        if self._documentation_retriever is None:
            return {"documentation_results": []}
        try:
            results = self._documentation_retriever.search(
                state["normalized_query"], limit=6
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
            generated = self._llm.answer_documentation_question(
                state["normalized_query"], chunks
            )
            if not set(generated.citations).issubset(allowed_urls):
                raise ValueError("documentation answer contained unsupported citation")
            answer = generated.answer
            citations = generated.citations
            suggestions = list(getattr(generated, "suggestions", []))
        except Exception:
            LOGGER.warning(
                "Documentation answer model unavailable; using extractive answer"
            )
            best = chunks[0]
            answer = self._extractive_documentation_answer(best)
            citations = [best["source_url"]]
            suggestions = []
        return {
            "answer": answer,
            "citations": citations,
            "suggestions": suggestions,
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
        try:
            context = self._retriever.search(
                query_text=state["user_query"],
                query_analysis=analysis,
                limit=5,
                debug=bool(state.get("debug")),
            )
        except Exception:
            # Asking the user for details cannot help while the incident graph
            # is unreachable; request_clarification reports the outage instead.
            LOGGER.exception("Incident evidence retrieval failed")
            return {
                "retrieved_context": {},
                "top_similarity_score": None,
                "evidence_unavailable": True,
            }
        context = self._verify_matches(state, context)
        scores = [
            float(issue["score"])
            for issue in context.get("matched_issues", [])
            if issue.get("score") is not None
        ]
        return {
            "retrieved_context": context,
            "top_similarity_score": max(scores) if scores else None,
        }

    def _verify_matches(
        self, state: IncidentState, context: dict[str, Any]
    ) -> dict[str, Any]:
        """Let the model drop or demote candidates that only look similar.

        Deterministic reranking cannot tell "same component, different failure"
        from a real match. Verification can only narrow the retrieved set, and
        any failure keeps the deterministic result.
        """

        verifier = getattr(self._llm, "verify_incident_matches", None)
        matched = context.get("matched_issues", [])
        related = context.get("related_issues", [])
        if verifier is None or not (matched or related):
            return context
        candidates = [
            {
                "issue_number": issue["issue_number"],
                "title": issue.get("title"),
                "summary": issue.get("summary"),
                "symptom": issue.get("symptom"),
                "components": (issue.get("components") or issue.get("shared_components") or [])[:8],
            }
            for issue in [*matched, *related]
        ]
        report = state.get("original_query") or state["user_query"]
        history = state.get("clarification_history") or []
        if history:
            report = "\n".join([report, *history])
        try:
            labels = verifier(report, candidates)
        except Exception:
            LOGGER.warning("Incident match verification unavailable; keeping reranked matches")
            return context
        verified = [i for i in matched if labels.get(i["issue_number"]) == "same_failure"]
        demoted = [i for i in matched if labels.get(i["issue_number"]) == "related"]
        if not verified:
            # Labels vary between runs for borderline wording. Without a
            # same-failure match, keep the rule-accepted "related" matches as
            # direct ones rather than discarding the only evidence; only
            # "unrelated" candidates are removed outright.
            verified, demoted = demoted, []
        kept_related = [
            i for i in related if labels.get(i["issue_number"]) in {"same_failure", "related"}
        ]
        LOGGER.warning(
            "Match verification kept=%d demoted=%d dropped=%d",
            len(verified),
            len(demoted),
            len(matched) + len(related) - len(verified) - len(demoted) - len(kept_related),
        )
        updated = {
            **context,
            "matched_issues": verified,
            "related_issues": [*demoted, *kept_related][:3] if verified else [],
            "match_verification": labels,
        }
        if not verified:
            updated["no_close_match"] = True
        return updated

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
            generated = self._llm.generate_grounded_fix(
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
        context = state.get("retrieved_context", {})
        fixed_issues = [
            {
                **self._issue_facts(issue),
                "resolution_summary": (issue.get("actionable_fix") or {}).get(
                    "resolution_summary"
                ),
            }
            for issue in [
                *context.get("matched_issues", []),
                *context.get("related_issues", []),
            ]
            if (issue.get("actionable_fix") or {}).get("resolution_summary") == answer
        ]
        composed = self._converse(
            state,
            "incident_fix",
            {
                "matched_issues": fixed_issues,
                "fix_summary_shown_below": answer,
                "verified_steps_shown_below": verified_steps,
            },
        )
        if composed is None:
            return {
                "answer": f"{answer}\nVerified steps:\n{steps}",
                "citations": citations,
                "suggestions": [],
            }
        # The summary and steps stay verbatim from the evidence; only the lead
        # sentence is model-written.
        return {
            "answer": f"{composed.reply}\n\n{answer}\n\n**Verified steps:**\n{steps}",
            "citations": self._unique([*citations, *composed.citations]),
            "suggestions": composed.suggestions,
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
        composed = self._converse(
            state,
            "incident_findings",
            {
                "known_details": self._known_details(state),
                "direct_matches": [self._issue_facts(issue) for issue in matched],
                "related_issues": [self._issue_facts(issue) for issue in related],
                "verified_fix_available": False,
            },
        )
        if composed is None:
            return {
                "answer": "\n".join(answer_parts),
                "citations": citations,
                "suggestions": [],
            }
        details = [composed.reply]
        if matched:
            details.append(
                "**Closest reported issues:**\n"
                + "\n".join(self._format_finding(issue) for issue in matched)
            )
        if related:
            details.append(
                "**Related through the graph** (not direct matches):\n"
                + "\n".join(self._format_finding(issue) for issue in related)
            )
        return {
            "answer": "\n\n".join(details),
            "citations": self._unique([*citations, *composed.citations]),
            "suggestions": composed.suggestions,
        }

    def request_clarification(self, state: IncidentState) -> dict[str, Any]:
        if state.get("query_understanding_unavailable"):
            # Asking the user for more detail cannot help while the semantic
            # router is down, so end the turn with an honest status instead.
            return {
                "answer": (
                    "The query-understanding service is temporarily unavailable, "
                    "so I could not interpret your request safely. Please try "
                    "again shortly."
                ),
                "citations": [],
                "route": "request_clarification",
                "route_reason": "query understanding service unavailable",
                "actionable_fix_available": False,
            }
        if state.get("evidence_unavailable"):
            return {
                "answer": (
                    "The incident knowledge graph is temporarily unavailable, so "
                    "I could not search historical incidents. Please try again "
                    "shortly."
                ),
                "citations": [],
                "route": "request_clarification",
                "route_reason": "incident evidence retrieval unavailable",
                "actionable_fix_available": False,
            }
        if state.get("out_of_scope"):
            return {
                "answer": state.get("clarification_question") or OUT_OF_SCOPE_ANSWER,
                "citations": [],
                "suggestions": state.get("suggestions", []),
                "route": "request_clarification",
                "route_reason": "query outside the assistant's scope",
                "actionable_fix_available": False,
            }
        missing = self._clarification_details(
            state,
            after_no_match=bool(
                state.get("retrieved_context", {}).get("no_close_match")
            ),
        )
        question = state.get("clarification_question") or self._template_question(
            state, missing
        )
        citations = list(state.get("citations") or []) if state.get(
            "clarification_question"
        ) else []
        suggestions = list(state.get("suggestions") or [])
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
                    "suggestions": [],
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
                    "citations": citations,
                    "suggestions": suggestions,
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
                "suggestions": [],
                "clarification_question": "",
            }
        return {
            "answer": question,
            "citations": citations,
            "suggestions": suggestions,
            "route": "request_clarification",
            "route_reason": state.get(
                "route_reason",
                "incident description lacks sufficient diagnostic detail",
            ),
            "actionable_fix_available": False,
        }

    def prepare_clarification(self, state: IncidentState) -> dict[str, Any]:
        """Compose the conversational clarification before the interrupt.

        ``request_clarification`` pauses with ``interrupt()`` and LangGraph
        re-runs that whole node on resume, so the model call lives here and
        runs once per clarification round.
        """

        empty = {"clarification_question": "", "suggestions": [], "citations": []}
        if state.get("query_understanding_unavailable") or state.get(
            "evidence_unavailable"
        ):
            return empty
        if state.get("out_of_scope"):
            composed = self._converse(state, "out_of_scope", {})
            if composed is None:
                return empty
            return {
                "clarification_question": composed.reply,
                "suggestions": composed.suggestions,
                "citations": [],
            }
        if (
            self._enable_interrupts
            and state.get("clarification_attempts", 0) >= 3
        ):
            return empty
        context = state.get("retrieved_context", {})
        missing = self._clarification_details(
            state, after_no_match=bool(context.get("no_close_match"))
        )
        facts: dict[str, Any] = {
            "known_details": self._known_details(state),
            "missing_details": missing,
            "no_close_match": bool(context.get("no_close_match")),
            "diagnostic_commands": DIAGNOSTIC_COMMANDS,
        }
        if context.get("cross_version_matches"):
            facts["cross_version_note"] = (
                "A similar symptom was reported in another Kubernetes version; "
                "its resolution must not be assumed to apply."
            )
        documentation = self._clarification_documentation(state)
        if documentation:
            facts["documentation"] = documentation
        composed = self._converse(state, "clarify", facts)
        if composed is None:
            return empty
        return {
            "clarification_question": composed.reply,
            "suggestions": composed.suggestions,
            "citations": composed.citations,
        }

    def _clarification_documentation(
        self, state: IncidentState
    ) -> list[dict[str, str]]:
        """A few doc chunks so a clarification can also offer a starting point.

        Only for reports that name a component or symptom; a bare "it's broken"
        would retrieve arbitrary pages.
        """

        if self._documentation_retriever is None:
            return []
        if not (state.get("extracted_components") or state.get("extracted_symptom")):
            return []
        try:
            chunks = self._documentation_retriever.search(
                state["normalized_query"], limit=3
            )
        except Exception:
            LOGGER.warning("Documentation lookup for clarification failed")
            return []
        return [
            {
                "title": chunk.get("title", ""),
                "text": chunk.get("text", "")[:COMPOSE_TEXT_CHARS * 2],
                "source_url": chunk["source_url"],
            }
            for chunk in chunks
            if chunk.get("source_url")
        ]

    def _template_question(self, state: IncidentState, missing: list[str]) -> str:
        """Deterministic clarification used when the composer is unavailable."""

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
        return question

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
            # The retriever already applied deterministic component, version, error,
            # and symptom-term reranking; its acceptance is the authoritative check.
            if (issue.get("relevance") or {}).get("accepted"):
                return True
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

    @classmethod
    def _analytics_cypher(
        cls,
        understanding: QueryUnderstanding,
    ) -> CypherGeneration | None:
        if understanding.extra_conditions:
            # Templates filter only by component and version; dropping a
            # condition such as "has a verified fix" would answer a different
            # question, so the model writes the query against the full schema.
            return None
        operation = understanding.analytics_operation
        group_by = understanding.group_by
        # One row beyond the displayed limit shows whether the list was cut short.
        limit = cls._display_limit(understanding) + 1
        version_condition, version_parameters = cls._version_condition(
            understanding.versions
        )
        version_match = (
            "MATCH (i)-[:REPORTED_IN_VERSION]->(v:KubernetesVersion) "
            f"WHERE {version_condition} "
            if version_condition
            else ""
        )
        if (
            operation == "TOP_N"
            and group_by == "component"
            and len(understanding.components) == 1
        ):
            # Ranking a single named component is meaningless: "the most common
            # kubelet issues" asks for that component's top issues.
            group_by = "issue"
        if operation == "COUNT" and group_by is None and understanding.components:
            # "How many issues affect the CSI driver?" counts per named component.
            group_by = "component"
        if operation in {"TOP_N", "COUNT", "GROUP_BY"} and group_by == "component":
            parameters = dict(version_parameters)
            component_filter = ""
            if understanding.components:
                # Named components restrict the counts ("kubelet or etcd?");
                # without them every component is ranked.
                component_filter = f"WHERE {COMPONENT_KEY} IN $components "
                parameters["components"] = list(dict.fromkeys(
                    cls._component_key(component)
                    for component in understanding.components
                ))
            return CypherGeneration(
                cypher=(
                    "MATCH (i:Issue)-[:AFFECTS_COMPONENT]->(c:Component) "
                    + component_filter
                    + version_match
                    + "RETURN c.name AS component, count(DISTINCT i) AS issue_count "
                    f"ORDER BY issue_count DESC LIMIT {limit}"
                ),
                parameters=parameters,
                explanation="Count distinct issues per component",
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
        # Two or more named components mean "issues involving all of them",
        # whether the model called it a relationship or a list.
        if operation in {"RELATIONSHIP", "LIST"} and len(understanding.components) >= 2:
            components = list(dict.fromkeys(
                cls._component_key(component) for component in understanding.components
            ))
            return CypherGeneration(
                cypher=(
                    "MATCH (i:Issue)-[:AFFECTS_COMPONENT]->(c:Component) "
                    f"WHERE {COMPONENT_KEY} IN $components "
                    f"WITH i, count(DISTINCT {COMPONENT_KEY}) AS matched_components "
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
        # A LIST without a grouping still asks for issues ("list the issues
        # affecting the scheduler"); leaving it to model Cypher made the result
        # depend on how the model chose to match the component.
        if (operation == "TOP_N" and group_by == "issue") or (
            operation == "LIST" and group_by in {None, "issue"}
        ):
            parameters: dict[str, Any] = dict(version_parameters)
            component_match = ""
            if understanding.components:
                component_match = (
                    "MATCH (i)-[:AFFECTS_COMPONENT]->(c:Component) "
                    f"WHERE {COMPONENT_KEY} = $component "
                )
                parameters["component"] = cls._component_key(
                    understanding.components[0]
                )
            return CypherGeneration(
                cypher=(
                    "MATCH (i:Issue) "
                    + component_match
                    + version_match
                    + "WITH DISTINCT i "
                    "RETURN i.issue_number AS issue_number, i.title AS title, "
                    "i.summary AS summary, i.html_url AS url, "
                    "coalesce(i.comments_count, 0) AS discussion_count "
                    f"ORDER BY discussion_count DESC LIMIT {limit}"
                ),
                parameters=parameters,
                explanation="List issues matching structured component and version filters",
            )
        return None

    @staticmethod
    def _component_key(name: str) -> str:
        """Match ``scheduler`` and ``kube-scheduler`` as the same component."""

        key = name.strip().lower()
        return key.removeprefix("kube-")

    @staticmethod
    def _display_limit(understanding: QueryUnderstanding) -> int:
        return min(understanding.limit or 20, CypherValidator.MAX_LIMIT - 1)

    @staticmethod
    def _version_condition(versions: list[str]) -> tuple[str, dict[str, Any]]:
        """Match a requested version at minor granularity.

        Graph versions are stored as reported (``1.35``, ``v1.35.1``, ``v1.35.x``),
        so exact equality with a user's ``v1.35`` would silently miss most issues.
        """

        if not versions:
            return "", {}
        match = re.search(r"(\d+)\.(\d+)", versions[0])
        if match is None:
            return "v.version = $version", {"version": versions[0]}
        return "v.version =~ $version_pattern", {
            "version_pattern": rf"^[vV]?{match[1]}\.{match[2]}(?:\D.*)?$"
        }
