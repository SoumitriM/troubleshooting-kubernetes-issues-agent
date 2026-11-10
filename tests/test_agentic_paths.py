"""Bounded agentic behaviour: broader analytics retries and multi-source research."""

from typing import Any

from app.agent.nodes import AgentSettings, IncidentNodes
from app.agent.state import CypherGeneration, QueryUnderstanding, ResearchStep
from app.agent.workflow import build_workflow


ISSUE_URL = "https://github.com/kubernetes/kubernetes/issues/141293"
DOC_URL = "https://kubernetes.io/docs/concepts/scheduling-eviction/dynamic-resource-allocation/"


def understanding(**overrides: Any) -> QueryUnderstanding:
    values = {
        "normalized_query": "q",
        "intent": "GRAPH_ANALYTICS",
        "intent_confidence": 0.9,
        "reasoning": "test",
    }
    return QueryUnderstanding(**{**values, **overrides})


class Repository:
    def __init__(self, results: list[list[dict[str, Any]]]) -> None:
        self.results = results
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute_read(self, cypher: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((cypher, parameters))
        if "MATCH (c:Component) WHERE any(token" in cypher:
            return [{"name": "draadminaccess"}]
        return self.results.pop(0) if self.results else []


class AlternativeLLM:
    def __init__(self) -> None:
        self.hints: list[str] = []

    def analyze_query(self, query: str) -> QueryUnderstanding:
        return understanding(
            normalized_query=query, components=["DRA admin access"], analytics_operation="LIST"
        )

    def generate_cypher(self, normalized, *, intent, validation_errors, hints=None):
        self.hints = hints or []
        return CypherGeneration(
            cypher="MATCH (i:Issue)-[:AFFECTS_COMPONENT]->(c:Component) "
            "WHERE c.name = $name RETURN i.issue_number AS issue_number, "
            "i.title AS title, i.html_url AS url LIMIT 20",
            parameters={"name": "draadminaccess"},
            explanation="Use the graph's component name",
        )


def test_empty_template_result_tries_one_broader_query_with_graph_hints() -> None:
    repository = Repository([[], [{"issue_number": 141293, "title": "adminAccess escalation", "url": ISSUE_URL}]])
    llm = AlternativeLLM()
    workflow = build_workflow(IncidentNodes(
        llm, None, AgentSettings(_env_file=None), analytics_repository=repository,  # type: ignore[arg-type]
    ))

    result = workflow.invoke({"user_query": "List the issues about DRA admin access"})

    assert result["route"] == "answer_from_graph"
    assert result["answer"].startswith("No reported issues matched the exact names")
    assert "#141293" in result["answer"]
    assert result["citations"] == [ISSUE_URL]
    assert any("draadminaccess" in hint for hint in llm.hints)


def test_broader_query_that_also_finds_nothing_says_so_once() -> None:
    repository = Repository([[], []])
    workflow = build_workflow(IncidentNodes(
        AlternativeLLM(), None, AgentSettings(_env_file=None), analytics_repository=repository,  # type: ignore[arg-type]
    ))

    result = workflow.invoke({"user_query": "List the issues about DRA admin access"})

    assert "or a broader search" in result["answer"]
    executed = [call for call in repository.calls if "any(token" not in call[0]]
    assert len(executed) == 2  # the template, then exactly one alternative


class ResearchLLM:
    def __init__(self, steps: list[ResearchStep]) -> None:
        self.steps = steps
        self.observations: list[list[dict[str, Any]]] = []

    def analyze_query(self, query: str) -> QueryUnderstanding:
        return understanding(
            normalized_query=query,
            intent="INCIDENT_TROUBLESHOOTING",
            has_concrete_incident=True,
            components=["dra"],
            sources=["incidents", "docs"],
        )

    def research_step(self, question, observations, *, remaining_tool_calls, allowed_urls):
        self.observations.append(list(observations))
        step = self.steps.pop(0)
        if step.action == "final_answer":
            assert set(step.citations) <= allowed_urls
        return step


class Retriever:
    def search(self, **_kwargs: Any) -> dict[str, Any]:
        return {"matched_issues": [{"issue_number": 141293, "title": "adminAccess escalation", "url": ISSUE_URL, "score": 0.9}]}


class Docs:
    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return [{"source_url": DOC_URL, "section": "Admin access", "text": "Admin access requires a namespace label."}]


def test_multi_source_question_runs_bounded_research_loop() -> None:
    llm = ResearchLLM([
        ResearchStep(action="search_incidents", reason="known issue?", query="namespace label grants adminAccess"),
        ResearchStep(action="search_docs", reason="what docs say", query="DRA admin access"),
        ResearchStep(action="final_answer", reason="covered", answer="Known issue #141293. Docs: admin access requires a namespace label.", citations=[ISSUE_URL, DOC_URL]),
    ])
    workflow = build_workflow(IncidentNodes(
        llm, Retriever(), AgentSettings(_env_file=None), documentation_retriever=Docs(),  # type: ignore[arg-type]
    ))

    result = workflow.invoke({"user_query": "Is DRA admin access escalation a known issue and what do the docs say?"})

    assert result["route"] == "research_answer"
    assert result["citations"] == [ISSUE_URL, DOC_URL]
    assert [step["action"] for step in result["research_steps"]] == ["search_incidents", "search_docs", "final_answer"]
    assert llm.observations[-1][0]["result"][0]["issue_number"] == 141293


def test_research_failure_falls_back_to_the_primary_route() -> None:
    class FailingLLM(ResearchLLM):
        def research_step(self, *args: Any, **kwargs: Any):
            raise RuntimeError("model unavailable")

    workflow = build_workflow(IncidentNodes(
        FailingLLM([]), Retriever(), AgentSettings(_env_file=None), documentation_retriever=Docs(),  # type: ignore[arg-type]
    ))

    result = workflow.invoke({"user_query": "Is DRA admin access escalation a known issue and what do the docs say?"})

    assert result["research_failed"] is True
    assert result["route"] in {"generate_findings", "request_clarification"}
