from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from app.agent.nodes import IncidentNodes
from app.agent.state import IncidentState, Intent, Route


def build_workflow(
    nodes: IncidentNodes,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
):
    """Compile the routed Kubernetes knowledge-assistant graph."""

    graph = StateGraph(IncidentState)
    graph.add_node("normalize_query", nodes.normalize_query)
    graph.add_node("classify_intent", nodes.classify_intent)
    graph.add_node("analyze_incident", nodes.analyze_incident)
    graph.add_node("retrieve_evidence", nodes.retrieve_evidence)
    graph.add_node("assess_evidence", nodes.assess_evidence)
    graph.add_node("generate_fix", nodes.generate_fix)
    graph.add_node("generate_findings", nodes.generate_findings)
    graph.add_node("request_clarification", nodes.request_clarification)
    graph.add_node("generate_cypher", nodes.generate_cypher)
    graph.add_node("validate_cypher", nodes.validate_cypher)
    graph.add_node("execute_neo4j", nodes.execute_neo4j)
    graph.add_node("answer_from_graph", nodes.answer_from_graph)
    graph.add_node("graph_query_failed", nodes.graph_query_failed)
    graph.add_node("retrieve_kubernetes_docs", nodes.retrieve_kubernetes_docs)
    graph.add_node(
        "answer_from_kubernetes_docs", nodes.answer_from_kubernetes_docs
    )
    graph.add_edge(START, "normalize_query")
    graph.add_edge("normalize_query", "classify_intent")
    graph.add_conditional_edges(
        "classify_intent",
        route_after_intent,
        {
            "INCIDENT_TROUBLESHOOTING": "analyze_incident",
            "GRAPH_ANALYTICS": "generate_cypher",
            "GENERAL_KUBERNETES_QUESTION": "retrieve_kubernetes_docs",
            "NEEDS_CLARIFICATION": "request_clarification",
        },
    )
    graph.add_conditional_edges(
        "analyze_incident",
        route_after_analysis,
        {
            "retrieve_evidence": "retrieve_evidence",
            "request_clarification": "request_clarification",
        },
    )
    graph.add_edge("retrieve_evidence", "assess_evidence")
    graph.add_conditional_edges(
        "assess_evidence",
        route_after_assessment,
        {
            "generate_fix": "generate_fix",
            "generate_findings": "generate_findings",
            "request_clarification": "request_clarification",
        },
    )
    graph.add_edge("generate_fix", END)
    graph.add_edge("generate_findings", END)
    graph.add_edge("generate_cypher", "validate_cypher")
    graph.add_conditional_edges(
        "validate_cypher",
        route_after_cypher_validation,
        {
            "regenerate": "generate_cypher",
            "execute": "execute_neo4j",
            "fail": "graph_query_failed",
        },
    )
    graph.add_edge("execute_neo4j", "answer_from_graph")
    graph.add_edge("answer_from_graph", END)
    graph.add_edge("graph_query_failed", END)
    graph.add_edge("retrieve_kubernetes_docs", "answer_from_kubernetes_docs")
    graph.add_edge("answer_from_kubernetes_docs", END)
    graph.add_conditional_edges(
        "request_clarification",
        route_after_clarification,
        {"normalize_query": "normalize_query", "end": END},
    )
    return graph.compile(checkpointer=checkpointer)


def route_after_assessment(state: IncidentState) -> Route:
    return state["route"]


def route_after_intent(state: IncidentState) -> Intent:
    return state["intent"]


def route_after_cypher_validation(state: IncidentState) -> str:
    if not state.get("cypher_validation_errors"):
        return "execute"
    if state.get("cypher_attempt_count", 0) < 2:
        return "regenerate"
    return "fail"


def route_after_analysis(state: IncidentState) -> str:
    return (
        "retrieve_evidence"
        if state["query_analysis"].has_sufficient_details
        else "request_clarification"
    )


def route_after_clarification(state: IncidentState) -> str:
    if state.get("clarification_exhausted") or state.get("answer"):
        return "end"
    return "normalize_query"
