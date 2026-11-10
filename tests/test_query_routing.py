import pytest

from app.agent.nodes import AgentSettings, IncidentNodes
from app.agent.state import QueryUnderstanding


class SemanticModel:
    def __init__(self, analysis: QueryUnderstanding) -> None:
        self.analysis = analysis
        self.calls = 0

    def analyze_query(self, query: str) -> QueryUnderstanding:
        self.calls += 1
        return self.analysis.model_copy(update={"normalized_query": query})


def understand(query: str, **updates):
    analysis = QueryUnderstanding(
        normalized_query=query,
        intent=updates.pop("intent"),
        intent_confidence=0.97,
        reasoning="Semantic interpretation supplied by Claude",
        **updates,
    )
    model = SemanticModel(analysis)
    result = IncidentNodes(  # type: ignore[arg-type]
        model, None, AgentSettings(_env_file=None)
    ).understand_query({"user_query": query})
    assert model.calls == 1
    return result


@pytest.mark.parametrize(
    "query",
    [
        "My kubelet keeps crashing after an upgrade.",
        "My kubelet keeps dying after an upgrade.",
        "The kubelet repeatedly terminates after upgrading.",
        "After the upgrade the node agent keeps going down.",
    ],
)
def test_semantic_incident_variations_route_identically(query: str) -> None:
    result = understand(
        query,
        intent="INCIDENT_TROUBLESHOOTING",
        components=["kubelet"],
        symptoms=["kubelet repeatedly terminates"],
        observed_behavior="kubelet repeatedly terminates after an upgrade",
        has_concrete_incident=True,
    )
    assert result["intent"] == "INCIDENT_TROUBLESHOOTING"
    assert result["has_sufficient_details"] is True


@pytest.mark.parametrize(
    "query",
    [
        "Which issues affect both kubelet and etcd?",
        "Which issues involve both kubelet and etcd?",
        "Show incidents connected to both kubelet and etcd.",
        "Find issues shared by kubelet and etcd.",
    ],
)
def test_semantic_relationship_variations_route_identically(query: str) -> None:
    result = understand(
        query,
        intent="GRAPH_ANALYTICS",
        components=["kubelet", "etcd"],
        analytics_operation="RELATIONSHIP",
        group_by="issue",
    )
    assert result["intent"] == "GRAPH_ANALYTICS"
    assert result["query_understanding"].analytics_operation == "RELATIONSHIP"


@pytest.mark.parametrize(
    "query",
    [
        "List the top 5 components by issue count.",
        "Which five components occur most often?",
        "Show me the five most frequently affected components.",
        "Rank components by number of incidents and return five.",
    ],
)
def test_semantic_top_n_variations_share_structured_operation(query: str) -> None:
    result = understand(
        query,
        intent="GRAPH_ANALYTICS",
        analytics_operation="TOP_N",
        group_by="component",
        limit=5,
    )
    analysis = result["query_understanding"]
    assert analysis.analytics_operation == "TOP_N"
    assert analysis.group_by == "component"
    assert analysis.limit == 5


def test_conceptual_query_routes_to_documentation() -> None:
    result = understand(
        "How does the scheduler decide where a Pod runs?",
        intent="GENERAL_KUBERNETES_QUESTION",
        components=["scheduler"],
    )
    assert result["intent"] == "GENERAL_KUBERNETES_QUESTION"


def test_model_outage_fails_closed_without_regex_nlp() -> None:
    class Unavailable:
        def analyze_query(self, query):
            raise ConnectionError("Claude unavailable")

    result = IncidentNodes(  # type: ignore[arg-type]
        Unavailable(), None, AgentSettings(_env_file=None)
    ).understand_query({"user_query": "My kubelet keeps dying"})

    assert result["intent"] == "NEEDS_CLARIFICATION"
    assert result["intent_confidence"] == 0.0
    assert "temporarily unavailable" in result["intent_reasoning"]


def test_lost_technical_token_keeps_semantics_but_uses_original_text() -> None:
    class RewritingModel:
        def analyze_query(self, query):
            return QueryUnderstanding(
                normalized_query="Kubelet crashes after upgrading Kubernetes",
                intent="INCIDENT_TROUBLESHOOTING",
                intent_confidence=0.95,
                components=["kubelet"],
                versions=["v1.35"],
                has_concrete_incident=True,
                reasoning="Concrete kubelet crash",
            )

    query = "My kubelet started crashing after upgrading to v1.35"
    result = IncidentNodes(  # type: ignore[arg-type]
        RewritingModel(), None, AgentSettings(_env_file=None)
    ).understand_query({"user_query": query})

    assert result["intent"] == "INCIDENT_TROUBLESHOOTING"
    assert result["normalized_query"] == query
    assert result["kubernetes_version"] == "v1.35"


def test_model_outage_ends_with_unavailable_answer_instead_of_interrupt() -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    from app.agent.workflow import build_workflow

    class Unavailable:
        def analyze_query(self, query):
            raise ConnectionError("Claude unavailable")

    workflow = build_workflow(
        IncidentNodes(  # type: ignore[arg-type]
            Unavailable(), None, AgentSettings(_env_file=None), enable_interrupts=True
        ),
        checkpointer=InMemorySaver(),
    )
    result = workflow.invoke(
        {"user_query": "What does the kubelet do?"},
        {"configurable": {"thread_id": "outage"}},
    )

    assert "__interrupt__" not in result
    assert "temporarily unavailable" in result["answer"]
    assert "Please provide" not in result["answer"]
    assert result["intent"] == "NEEDS_CLARIFICATION"


def test_out_of_scope_query_gets_scope_answer_without_clarification() -> None:
    from app.agent.nodes import AgentSettings, IncidentNodes
    from app.agent.state import QueryUnderstanding
    from app.agent.workflow import build_workflow

    class OffTopicLLM:
        def analyze_query(self, query: str) -> QueryUnderstanding:
            return QueryUnderstanding(
                normalized_query=query,
                intent="NEEDS_CLARIFICATION",
                intent_confidence=0.9,
                in_scope=False,
                reasoning="Not a Kubernetes question",
            )

    workflow = build_workflow(
        IncidentNodes(OffTopicLLM(), None, AgentSettings(_env_file=None))  # type: ignore[arg-type]
    )

    result = workflow.invoke({"user_query": "What is the capital of France?"})

    assert result["route"] == "request_clarification"
    assert result["answer"].startswith("I can only help with Kubernetes")
    assert "Please provide" not in result["answer"]
