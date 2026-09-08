from app.agent.nodes import AgentSettings, IncidentNodes


class NoModelRouting:
    pass


class UnavailableModel:
    def normalize_query(self, query):
        raise ConnectionError("model endpoint unavailable")


def classify(query: str) -> str:
    nodes = IncidentNodes(  # type: ignore[arg-type]
        NoModelRouting(), None, AgentSettings(_env_file=None)
    )
    normalized = nodes.normalize_query({"user_query": query})
    return nodes.classify_intent(normalized)["intent"]


def test_incident_query_routes_to_existing_troubleshooting_flow() -> None:
    assert (
        classify("My kubelet started crashing after upgrading to v1.35")
        == "INCIDENT_TROUBLESHOOTING"
    )


def test_common_issues_query_routes_to_graph_analytics() -> None:
    assert (
        classify("What are the most common kubelet issues in v1.35?")
        == "GRAPH_ANALYTICS"
    )


def test_component_count_query_routes_to_graph_analytics() -> None:
    assert (
        classify("Which components have the most reported issues?")
        == "GRAPH_ANALYTICS"
    )


def test_conceptual_query_routes_to_general_knowledge() -> None:
    assert classify("What does kubelet do?") == "GENERAL_KUBERNETES_QUESTION"


def test_vague_query_fails_safe() -> None:
    assert classify("Something is broken") == "NEEDS_CLARIFICATION"


def test_obvious_route_survives_optional_model_outage() -> None:
    nodes = IncidentNodes(  # type: ignore[arg-type]
        UnavailableModel(), None, AgentSettings(_env_file=None)
    )
    normalized = nodes.normalize_query({"user_query": "What does kubelet do?"})
    assert nodes.classify_intent(normalized)["intent"] == "GENERAL_KUBERNETES_QUESTION"


def test_latest_clarification_controls_reclassification() -> None:
    nodes = IncidentNodes(  # type: ignore[arg-type]
        UnavailableModel(), None, AgentSettings(_env_file=None)
    )
    state = nodes.normalize_query(
        {
            "original_query": "Something is broken",
            "user_query": "Something is broken",
            "clarification_history": ["What does kubelet do?"],
        }
    )
    state["clarification_history"] = ["What does kubelet do?"]
    assert nodes.classify_intent(state)["intent"] == "GENERAL_KUBERNETES_QUESTION"
