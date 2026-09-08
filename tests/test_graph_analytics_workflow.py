from app.agent.nodes import AgentSettings, IncidentNodes
from app.agent.state import CypherGeneration, IntentClassification
from app.agent.workflow import build_workflow


class AnalyticsModel:
    def __init__(self, queries: list[str]) -> None:
        self.queries = iter(queries)
        self.feedback: list[list[str]] = []

    def generate_cypher(self, normalized, *, intent, validation_errors):
        assert intent == "GRAPH_ANALYTICS"
        self.feedback.append(validation_errors)
        return CypherGeneration(
            cypher=next(self.queries),
            parameters={"component": "kubelet"},
            explanation="Count issues by component",
        )

    def classify_query(self, normalized):
        return IntentClassification(
            intent="GRAPH_ANALYTICS",
            confidence=0.9,
            reasoning="The query asks for graph relationships",
        )

    def answer_graph_question(self, query, rows):
        return f"Found {rows[0]['reports']} reports for kubelet."


class AnalyticsRepository:
    def __init__(self) -> None:
        self.calls = []

    def execute_read(self, cypher, parameters):
        self.calls.append((cypher, parameters))
        return [{"component": "kubelet", "reports": 12}]


def graph_nodes(model, repository):
    return IncidentNodes(  # type: ignore[arg-type]
        model,
        None,
        AgentSettings(_env_file=None),
        analytics_repository=repository,
    )


def test_invalid_cypher_is_regenerated_then_executed() -> None:
    model = AnalyticsModel(
        [
            "MATCH (i:Issue)-[:UNKNOWN]->(c:Component) RETURN c LIMIT 10",
            (
                "MATCH (i:Issue)-[:AFFECTS_COMPONENT]->(c:Component) "
                "WHERE c.name = $component RETURN c.name AS component, "
                "count(i) AS reports ORDER BY reports DESC LIMIT 10"
            ),
        ]
    )
    repository = AnalyticsRepository()
    result = build_workflow(graph_nodes(model, repository)).invoke(
        {"user_query": "Analyze graph relationships for kubelet"}
    )
    assert result["cypher_attempt_count"] == 2
    assert model.feedback[1]
    assert repository.calls[0][1] == {"component": "kubelet"}
    assert result["answer"] == "Found 12 reports for kubelet."


def test_retry_limit_prevents_execution_of_unsafe_query() -> None:
    model = AnalyticsModel(
        [
            "MATCH (i:Issue) DELETE i RETURN i LIMIT 10",
            "MATCH (i:Issue) DELETE i RETURN i LIMIT 10",
        ]
    )
    repository = AnalyticsRepository()
    result = build_workflow(graph_nodes(model, repository)).invoke(
        {"user_query": "Analyze graph relationships for kubelet"}
    )
    assert result["cypher_attempt_count"] == 2
    assert repository.calls == []
    assert "safe graph query after two attempts" in result["answer"]


class UnavailableAnalyticsModel:
    def generate_cypher(self, *args, **kwargs):
        raise AssertionError("deterministic analytics must not call the model")

    def answer_graph_question(self, query, rows):
        return f"Returned {len(rows)} graph rows."


def test_common_issue_analytics_works_without_model_endpoint() -> None:
    repository = AnalyticsRepository()
    result = build_workflow(
        graph_nodes(UnavailableAnalyticsModel(), repository)
    ).invoke(
        {"user_query": "What are the most common kubelet issues in v1.35?"}
    )

    cypher, parameters = repository.calls[0]
    assert "AFFECTS_COMPONENT" in cypher
    assert "REPORTED_IN_VERSION" in cypher
    assert parameters == {"component": "kubelet", "version": "v1.35"}
    assert result["route"] == "answer_from_graph"


def test_component_counts_work_without_model_endpoint() -> None:
    repository = AnalyticsRepository()
    build_workflow(graph_nodes(UnavailableAnalyticsModel(), repository)).invoke(
        {"user_query": "Which components have the most reported issues?"}
    )

    cypher, parameters = repository.calls[0]
    assert "count(DISTINCT i) AS issue_count" in cypher
    assert parameters == {}
