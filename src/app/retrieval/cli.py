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

from app.graph.loader import GraphSettings
from app.retrieval.embeddings import IssueEmbedder
from app.retrieval.graph_retriever import GraphRetriever
from app.retrieval.vector_store import IssueVectorStore


class _HuggingFaceAuthenticationNoticeFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage().lower()
        return not (
            record.levelno == logging.WARNING
            and "unauthenticated" in message
            and any(
                name in message
                for name in ("hugging face", "huggingface", "hf hub")
            )
        )


def _configure_cli_diagnostics() -> None:
    notice_filter = _HuggingFaceAuthenticationNoticeFilter()
    logging.getLogger("huggingface_hub").addFilter(notice_filter)
    logging.getLogger("huggingface_hub.utils._http").addFilter(notice_filter)


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
    graph_search.add_argument(
        "--debug",
        action="store_true",
        help="Include accepted and rejected candidate relevance diagnostics",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    _configure_cli_diagnostics()
    with redirect_stdout(sys.stderr):
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
                output = {"indexed_changed_issues": store.index_issues()}
            elif args.command == "search":
                store = IssueVectorStore(driver, embedder)
                output = {
                    "query_mode": "raw_query_only",
                    "results": store.search(args.query, args.limit),
                }
            else:
                output = GraphRetriever(driver, embedder).search(
                    query_text=args.query,
                    query_analysis=None,
                    limit=args.limit,
                    debug=args.debug,
                )
        finally:
            driver.close()
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
