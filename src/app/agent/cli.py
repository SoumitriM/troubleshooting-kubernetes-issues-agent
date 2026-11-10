import argparse
import json
import logging
import os
import sys
from contextlib import redirect_stdout

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from neo4j import GraphDatabase

from app.agent.nodes import AgentSettings, IncidentNodes
from app.agent.workflow import build_workflow
from app.enrichment.claude_client import ClaudeClient, ClaudeSettings
from app.graph.loader import GraphSettings
from app.retrieval.embeddings import IssueEmbedder
from app.retrieval.graph_retriever import GraphRetriever


class _HuggingFaceAuthenticationNoticeFilter(logging.Filter):
    """Hide only the optional unauthenticated-download advisory."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage().lower()
        return not (
            record.levelno == logging.WARNING
            and "unauthenticated" in message
            and (
                "hugging face" in message
                or "huggingface" in message
                or "hf hub" in message
            )
        )


def _configure_cli_diagnostics() -> None:
    notice_filter = _HuggingFaceAuthenticationNoticeFilter()
    logging.getLogger("huggingface_hub").addFilter(notice_filter)
    logging.getLogger("huggingface_hub.utils._http").addFilter(notice_filter)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evidence-grounded incident assistant")
    parser.add_argument("--query", required=True)
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Include a sanitized LangGraph execution trace",
    )
    return parser


def _run_debug(workflow, query: str) -> tuple[dict, list[str]]:
    initial_state = {"user_query": query, "debug": True}
    state = dict(initial_state)
    executed_nodes: list[str] = []
    for update in workflow.stream(initial_state, stream_mode="updates"):
        for node_name, values in update.items():
            executed_nodes.append(node_name)
            if values:
                state.update(values)
    return state, executed_nodes


def _debug_output(result: dict, query: str, executed_nodes: list[str]) -> dict:
    context = result.get("retrieved_context", {})
    retrieval_debug = context.get("debug", {})
    matched = context.get("matched_issues", [])
    related = context.get("related_issues", [])
    query_analysis = result.get("query_analysis")
    analysis_data = (
        query_analysis.model_dump()
        if hasattr(query_analysis, "model_dump")
        else {
            "symptom": result.get("extracted_symptom"),
            "components": result.get("extracted_components", []),
            "kubernetes_version": result.get("kubernetes_version"),
            "error_messages": result.get("error_messages", []),
            "has_sufficient_details": result.get("has_sufficient_details", False),
            "missing_details": result.get("missing_details", []),
        }
    )
    reranking = [
        {
            "issue_number": detail.get("issue_number"),
            "vector_score": detail.get("vector_similarity"),
            "matching_components": detail.get("matching_components", []),
            "matching_versions": detail.get("matching_versions", []),
            "conflicting_versions": detail.get("conflicting_versions", []),
            "version_match_type": detail.get("version_match_type", "unknown"),
            "matching_error_terms": detail.get("matching_error_terms", []),
            "matching_symptom_terms": detail.get("matching_symptom_terms", []),
            "version_conflict": detail.get("version_conflict", False),
            "final_score": detail.get("final_relevance_score"),
            "accepted": detail.get("accepted", False),
            "cross_version_match": detail.get("cross_version_match", False),
            "cross_version_explanation": detail.get(
                "cross_version_explanation"
            ),
            "rejection_reason": ", ".join(detail.get("rejection_reasons", []))
            or None,
        }
        for detail in retrieval_debug.get("reranking", [])
    ]
    return {
        "route": result["route"],
        "answer": result["answer"],
        "citations": result["citations"],
        "debug": {
            "original_user_query": query,
            "executed_nodes": executed_nodes,
            "analyze_incident": analysis_data,
            "vector_candidates_before_reranking": retrieval_debug.get(
                "vector_candidates", []
            ),
            "reranking_details": reranking,
            "issues_after_reranking": [
                {
                    "issue_number": issue.get("issue_number"),
                    "vector_score": float(issue.get("score") or 0.0),
                    "final_score": (issue.get("relevance") or {}).get(
                        "final_relevance_score"
                    ),
                }
                for issue in matched
            ],
            "graph_expanded_related_issues": [
                {
                    "issue_number": issue.get("issue_number"),
                    "shared_components": issue.get("shared_components", []),
                    "shared_versions": issue.get("shared_versions", []),
                }
                for issue in related
            ],
            "cross_version_matches": [
                {
                    "issue_number": issue.get("issue_number"),
                    "candidate_versions": issue.get("kubernetes_versions", []),
                    "conflicting_versions": (issue.get("relevance") or {}).get(
                        "conflicting_versions", []
                    ),
                    "final_score": (issue.get("relevance") or {}).get(
                        "final_relevance_score"
                    ),
                    "explanation": (issue.get("relevance") or {}).get(
                        "cross_version_explanation"
                    ),
                }
                for issue in context.get("cross_version_matches", [])
            ],
            "evidence_assessment": {
                "top_similarity_score": result.get("top_similarity_score"),
                "actionable_fix_available": result.get(
                    "actionable_fix_available", False
                ),
                "decision": result.get("route"),
            },
            "final_route": result["route"],
            "route_reason": result.get("route_reason"),
        },
    }


def main() -> None:
    args = build_parser().parse_args()
    _configure_cli_diagnostics()
    # Third-party model loaders occasionally print progress to stdout. Keep stdout
    # reserved for the CLI's single JSON document while preserving diagnostics on
    # stderr and allowing genuine exceptions to propagate.
    with redirect_stdout(sys.stderr):
        graph_settings = GraphSettings()
        driver = GraphDatabase.driver(
            graph_settings.neo4j_uri,
            auth=(
                graph_settings.neo4j_username,
                graph_settings.neo4j_password.get_secret_value(),
            ),
        )
        try:
            llm = ClaudeClient(ClaudeSettings())
            retriever = GraphRetriever(driver, IssueEmbedder())
            workflow = build_workflow(
                IncidentNodes(llm, retriever, AgentSettings())
            )
            if args.debug:
                result, executed_nodes = _run_debug(workflow, args.query)
            else:
                result = workflow.invoke({"user_query": args.query})
        finally:
            driver.close()
    output = (
        _debug_output(result, args.query, executed_nodes)
        if args.debug
        else {
            "route": result["route"],
            "answer": result["answer"],
            "citations": result["citations"],
        }
    )
    print(json.dumps(output, ensure_ascii=False, indent=2 if args.debug else None))


if __name__ == "__main__":
    main()
