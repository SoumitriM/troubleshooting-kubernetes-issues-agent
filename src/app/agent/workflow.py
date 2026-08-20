from langgraph.graph import END, START, StateGraph

from app.agent.nodes import IncidentNodes
from app.agent.state import IncidentState, Route


def build_workflow(nodes: IncidentNodes):
    """Compile the evidence-grounded incident-assistant graph."""

    graph = StateGraph(IncidentState)
    graph.add_node("analyze_incident", nodes.analyze_incident)
    graph.add_node("retrieve_evidence", nodes.retrieve_evidence)
    graph.add_node("assess_evidence", nodes.assess_evidence)
    graph.add_node("generate_fix", nodes.generate_fix)
    graph.add_node("generate_findings", nodes.generate_findings)
    graph.add_node("request_clarification", nodes.request_clarification)
    graph.add_edge(START, "analyze_incident")
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
    graph.add_edge("request_clarification", END)
    return graph.compile()


def route_after_assessment(state: IncidentState) -> Route:
    return state["route"]


def route_after_analysis(state: IncidentState) -> str:
    return (
        "retrieve_evidence"
        if state["query_analysis"].has_sufficient_details
        else "request_clarification"
    )
