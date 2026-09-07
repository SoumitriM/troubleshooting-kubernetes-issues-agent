from app.agent.nodes import AgentSettings, IncidentNodes
from app.agent.state import CypherGeneration
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
        {"user_query": "What are the most common kubelet issues?"}
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
        {"user_query": "Which components have the most reported issues?"}
    )
    assert result["cypher_attempt_count"] == 2
    assert repository.calls == []
    assert "safe graph query after two attempts" in result["answer"]
