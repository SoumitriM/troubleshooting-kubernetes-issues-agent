from app.agent.nodes import AgentSettings, IncidentNodes
from app.agent.state import CypherGeneration, QueryUnderstanding
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

    def analyze_query(self, query):
        return QueryUnderstanding(
            normalized_query=query,
            intent="GRAPH_ANALYTICS",
            intent_confidence=0.9,
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
    def __init__(self, understanding):
        self.understanding = understanding

    def analyze_query(self, query):
        return self.understanding.model_copy(update={"normalized_query": query})

    def generate_cypher(self, *args, **kwargs):
        raise AssertionError("deterministic analytics must not call the model")

    def answer_graph_question(self, query, rows):
        return f"Returned {len(rows)} graph rows."


def test_common_issue_analytics_works_without_model_endpoint() -> None:
    repository = AnalyticsRepository()
    result = build_workflow(
        graph_nodes(UnavailableAnalyticsModel(QueryUnderstanding(
            normalized_query="placeholder",
            intent="GRAPH_ANALYTICS",
            intent_confidence=0.99,
            components=["kubelet"],
            versions=["v1.35"],
            analytics_operation="LIST",
            group_by="issue",
            reasoning="List matching issues",
        )), repository)
    ).invoke(
        {"user_query": "What are the most common kubelet issues in v1.35?"}
    )

    cypher, parameters = repository.calls[0]
    assert "AFFECTS_COMPONENT" in cypher
    assert "REPORTED_IN_VERSION" in cypher
    assert parameters == {
        "component": "kubelet",
        "version_pattern": r"^[vV]?1\.35(?:\D.*)?$",
    }
    assert result["route"] == "answer_from_graph"


def test_version_filter_matches_stored_version_variants() -> None:
    import re

    generated = IncidentNodes._analytics_cypher(QueryUnderstanding(
        normalized_query="placeholder",
        intent="GRAPH_ANALYTICS",
        intent_confidence=0.99,
        versions=["v1.35"],
        analytics_operation="TOP_N",
        group_by="issue",
        reasoning="Most discussed issues in a version",
    ))

    assert generated is not None
    pattern = re.compile(generated.parameters["version_pattern"])
    for stored in ["1.35", "v1.35", "v1.35.1", "v1.35.x", "V1.35.0-rc.0"]:
        assert pattern.fullmatch(stored), stored
    for stored in ["1.350", "v1.36.1", "v11.35"]:
        assert not pattern.fullmatch(stored), stored


def test_every_structured_template_passes_cypher_validation() -> None:
    from app.graph.cypher_validation import CypherValidator

    cases = [
        ("TOP_N", "component", ["kubelet"], ["v1.35"]),
        ("COUNT", "component", [], []),
        ("COUNT", "component", ["etcd"], ["v1.36"]),
        ("GROUP_BY", "version", [], []),
        ("RELATIONSHIP", "issue", ["kubelet", "etcd"], []),
        ("LIST", "issue", ["kubelet"], ["v1.35"]),
        ("TOP_N", "issue", [], ["1.36"]),
        ("LIST", "issue", [], []),
    ]
    for operation, group_by, components, versions in cases:
        generated = IncidentNodes._analytics_cypher(QueryUnderstanding(
            normalized_query="placeholder",
            intent="GRAPH_ANALYTICS",
            intent_confidence=0.99,
            components=components,
            versions=versions,
            analytics_operation=operation,
            group_by=group_by,
            reasoning="Structured analytics",
        ))
        assert generated is not None, (operation, group_by)
        result = CypherValidator().validate(generated.cypher, generated.parameters)
        assert result.valid, (operation, group_by, result.errors)


def test_component_counts_work_without_model_endpoint() -> None:
    repository = AnalyticsRepository()
    build_workflow(graph_nodes(UnavailableAnalyticsModel(QueryUnderstanding(
        normalized_query="placeholder",
        intent="GRAPH_ANALYTICS",
        intent_confidence=0.99,
        analytics_operation="GROUP_BY",
        group_by="component",
        reasoning="Count issues by component",
    )), repository)).invoke(
        {"user_query": "Which components have the most reported issues?"}
    )

    cypher, parameters = repository.calls[0]
    assert "count(DISTINCT i) AS issue_count" in cypher
    assert parameters == {}


def test_structured_relationship_uses_safe_template() -> None:
    generated = IncidentNodes._analytics_cypher(QueryUnderstanding(
        normalized_query="Any equivalent relationship wording",
        intent="GRAPH_ANALYTICS",
        intent_confidence=0.99,
        components=["kubelet", "etcd"],
        analytics_operation="RELATIONSHIP",
        group_by="issue",
        limit=10,
        reasoning="Issues connected to both components",
    ))

    assert generated is not None
    assert generated.parameters == {
        "components": ["kubelet", "etcd"],
        "component_count": 2,
    }
    assert "LIMIT 11" in generated.cypher  # one extra row detects truncation


def test_structured_top_n_honors_validated_limit() -> None:
    generated = IncidentNodes._analytics_cypher(QueryUnderstanding(
        normalized_query="Any equivalent ranking wording",
        intent="GRAPH_ANALYTICS",
        intent_confidence=0.99,
        analytics_operation="TOP_N",
        group_by="component",
        limit=5,
        reasoning="Rank components",
    ))

    assert generated is not None
    assert "count(DISTINCT i) AS issue_count" in generated.cypher
    assert "LIMIT 6" in generated.cypher  # one extra row detects truncation


def test_structured_version_grouping_uses_version_relationship() -> None:
    generated = IncidentNodes._analytics_cypher(QueryUnderstanding(
        normalized_query="Any equivalent version grouping wording",
        intent="GRAPH_ANALYTICS",
        intent_confidence=0.99,
        analytics_operation="GROUP_BY",
        group_by="version",
        reasoning="Count incidents by version",
    ))

    assert generated is not None
    assert "REPORTED_IN_VERSION" in generated.cypher
    assert "incident_count" in generated.cypher


def test_component_count_is_limited_to_named_components() -> None:
    generated = IncidentNodes._analytics_cypher(QueryUnderstanding(
        normalized_query="How many issues affect etcd?",
        intent="GRAPH_ANALYTICS",
        intent_confidence=0.99,
        components=["etcd"],
        analytics_operation="COUNT",
        group_by="component",
        reasoning="Count etcd issues",
    ))

    assert generated is not None
    assert "IN $components" in generated.cypher
    assert generated.parameters == {"components": ["etcd"]}


def test_top_n_for_one_named_component_lists_its_issues() -> None:
    generated = IncidentNodes._analytics_cypher(QueryUnderstanding(
        normalized_query="What are the most common kubelet issues in v1.36?",
        intent="GRAPH_ANALYTICS",
        intent_confidence=0.95,
        components=["kubelet"],
        versions=["v1.36"],
        analytics_operation="TOP_N",
        group_by="component",
        limit=10,
        reasoning="Most common kubelet issues",
    ))

    assert generated is not None
    assert "i.issue_number AS issue_number" in generated.cypher
    assert generated.parameters["component"] == "kubelet"
    assert "LIMIT 11" in generated.cypher  # one extra row detects truncation


def test_list_without_grouping_uses_the_issue_template() -> None:
    from app.agent.nodes import IncidentNodes
    from app.agent.state import QueryUnderstanding

    generated = IncidentNodes._analytics_cypher(QueryUnderstanding(
        normalized_query="List the issues affecting the scheduler",
        intent="GRAPH_ANALYTICS",
        intent_confidence=0.9,
        components=["scheduler"],
        reasoning="List issues",
        analytics_operation="LIST",
    ))

    assert generated is not None
    assert generated.parameters["component"] == "scheduler"
    assert "replace(toLower(c.name), 'kube-', '') = $component" in generated.cypher


def test_extra_conditions_bypass_templates() -> None:
    from app.agent.nodes import IncidentNodes
    from app.agent.state import QueryUnderstanding

    request = QueryUnderstanding(
        normalized_query="Which issues have a verified fix linked to a pull request?",
        intent="GRAPH_ANALYTICS",
        intent_confidence=0.9,
        reasoning="List issues with a condition",
        analytics_operation="LIST",
        extra_conditions=["has a verified fix", "linked to a pull request"],
    )

    assert IncidentNodes._analytics_cypher(request) is None
