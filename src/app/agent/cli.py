import argparse
import json

from neo4j import GraphDatabase

from app.agent.nodes import AgentSettings, IncidentNodes
from app.agent.workflow import build_workflow
from app.enrichment.fh_genie_client import EnrichmentSettings, FHGenieClient
from app.graph.loader import GraphSettings
from app.retrieval.embeddings import IssueEmbedder
from app.retrieval.graph_retriever import GraphRetriever


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evidence-grounded incident assistant")
    parser.add_argument("--query", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    graph_settings = GraphSettings()
    driver = GraphDatabase.driver(
        graph_settings.neo4j_uri,
        auth=(
            graph_settings.neo4j_username,
            graph_settings.neo4j_password.get_secret_value(),
        ),
    )
    try:
        fh_genie = FHGenieClient(EnrichmentSettings())
        retriever = GraphRetriever(driver, IssueEmbedder())
        workflow = build_workflow(
            IncidentNodes(fh_genie, retriever, AgentSettings())
        )
        result = workflow.invoke({"user_query": args.query})
    finally:
        driver.close()
    print(
        json.dumps(
            {
                "route": result["route"],
                "answer": result["answer"],
                "citations": result["citations"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
