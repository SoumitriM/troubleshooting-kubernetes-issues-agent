from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from app.agent.nodes import IncidentNodes
from app.agent.state import IncidentState, Route


def build_workflow(
    nodes: IncidentNodes,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
):
    """Compile the routed Kubernetes knowledge-assistant graph."""

    graph = StateGraph(IncidentState)
    graph.add_node("understand_query", nodes.understand_query)
    graph.add_node("analyze_incident", nodes.analyze_incident)
    graph.add_node("retrieve_evidence", nodes.retrieve_evidence)
    graph.add_node("assess_evidence", nodes.assess_evidence)
    graph.add_node("generate_fix", nodes.generate_fix)
    graph.add_node("generate_findings", nodes.generate_findings)
    graph.add_node("troubleshoot_from_docs", nodes.troubleshoot_from_docs)
    graph.add_node("prepare_clarification", nodes.prepare_clarification)
    graph.add_node("request_clarification", nodes.request_clarification)
    graph.add_node("generate_cypher", nodes.generate_cypher)
    graph.add_node("validate_cypher", nodes.validate_cypher)
    graph.add_node("execute_neo4j", nodes.execute_neo4j)
    graph.add_node("answer_from_graph", nodes.answer_from_graph)
    graph.add_node("graph_query_failed", nodes.graph_query_failed)
    graph.add_node("prepare_alternative", nodes.prepare_alternative)
    graph.add_node("research", nodes.research)
    graph.add_node("retrieve_kubernetes_docs", nodes.retrieve_kubernetes_docs)
    graph.add_node(
        "answer_from_kubernetes_docs", nodes.answer_from_kubernetes_docs
    )
    graph.add_edge(START, "understand_query")
    graph.add_conditional_edges(
        "understand_query",
        route_after_intent,
        {
            "INCIDENT_TROUBLESHOOTING": "analyze_incident",
            "GRAPH_ANALYTICS": "generate_cypher",
            "GENERAL_KUBERNETES_QUESTION": "retrieve_kubernetes_docs",
            "NEEDS_CLARIFICATION": "prepare_clarification",
            "RESEARCH": "research",
        },
    )
    graph.add_conditional_edges(
        "research",
        route_after_research,
        {
            "end": END,
            "INCIDENT_TROUBLESHOOTING": "analyze_incident",
            "GRAPH_ANALYTICS": "generate_cypher",
            "GENERAL_KUBERNETES_QUESTION": "retrieve_kubernetes_docs",
            "NEEDS_CLARIFICATION": "prepare_clarification",
        },
    )
    graph.add_conditional_edges(
        "analyze_incident",
        route_after_analysis,
        {
            "retrieve_evidence": "retrieve_evidence",
            "request_clarification": "prepare_clarification",
        },
    )
    graph.add_edge("retrieve_evidence", "assess_evidence")
    # The docs are the primary troubleshooting source for every incident;
    # GitHub evidence chosen by assess_evidence is attached to that answer.
    graph.add_edge("assess_evidence", "troubleshoot_from_docs")
    graph.add_conditional_edges(
        "troubleshoot_from_docs",
        route_after_docs_troubleshooting,
        {
            "end": END,
            # The docs could not explain the problem: use the GitHub-only route.
            "generate_fix": "generate_fix",
            "generate_findings": "generate_findings",
            "request_clarification": "prepare_clarification",
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
    graph.add_conditional_edges(
        "execute_neo4j",
        route_after_execution,
        {"alternative": "prepare_alternative", "answer": "answer_from_graph"},
    )
    graph.add_edge("prepare_alternative", "generate_cypher")
    graph.add_edge("answer_from_graph", END)
    graph.add_edge("graph_query_failed", END)
    graph.add_edge("retrieve_kubernetes_docs", "answer_from_kubernetes_docs")
    graph.add_edge("answer_from_kubernetes_docs", END)
    graph.add_edge("prepare_clarification", "request_clarification")
    graph.add_conditional_edges(
        "request_clarification",
        route_after_clarification,
        {"understand_query": "understand_query", "end": END},
    )
    return graph.compile(checkpointer=checkpointer)


def route_after_assessment(state: IncidentState) -> Route:
    return state["route"]


def route_after_intent(state: IncidentState) -> str:
    understanding = state.get("query_understanding")
    if (
        understanding is not None
        and understanding.in_scope
        and state["intent"] != "NEEDS_CLARIFICATION"
        and len(set(understanding.sources)) >= 2
    ):
        return "RESEARCH"
    return state["intent"]


def route_after_research(state: IncidentState) -> str:
    return state["intent"] if state.get("research_failed") else "end"


def route_after_execution(state: IncidentState) -> str:
    """Try one broader model-written query when the first query finds nothing."""

    if (
        not state.get("answer")
        and not state.get("graph_results")
        and not state.get("alternative_attempted")
    ):
        return "alternative"
    return "answer"


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
    return "understand_query"


def route_after_docs_troubleshooting(state: IncidentState) -> str:
    if not state.get("docs_troubleshooting_failed"):
        return "end"
    return state["route"]
