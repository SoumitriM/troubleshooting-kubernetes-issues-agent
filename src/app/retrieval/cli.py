import argparse
import json

from neo4j import GraphDatabase

from app.graph.loader import GraphSettings
from app.retrieval.embeddings import IssueEmbedder
from app.retrieval.graph_retriever import GraphRetriever
from app.retrieval.vector_store import IssueVectorStore


def positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Index and search Issue nodes")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("index", help="Embed changed Issue nodes")
    search = commands.add_parser("search", help="Search indexed Issue nodes")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=positive_integer, default=5)
    graph_search = commands.add_parser(
        "graph-search",
        help="Search Issue nodes and expand through graph relationships",
    )
    graph_search.add_argument("--query", required=True)
    graph_search.add_argument("--limit", type=positive_integer, default=5)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = GraphSettings()
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(
            settings.neo4j_username,
            settings.neo4j_password.get_secret_value(),
        ),
    )
    try:
        embedder = IssueEmbedder()
        if args.command == "index":
            store = IssueVectorStore(driver, embedder)
            count = store.index_issues()
            print(f"Indexed {count} changed issues")
        elif args.command == "search":
            store = IssueVectorStore(driver, embedder)
            for result in store.search(args.query, args.limit):
                print(json.dumps(result, ensure_ascii=False))
        else:
            context = GraphRetriever(driver, embedder).retrieve(args.query, args.limit)
            print(json.dumps(context, ensure_ascii=False))
    finally:
        driver.close()


if __name__ == "__main__":
    main()
