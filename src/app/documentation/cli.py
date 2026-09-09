"""Fetch, index, and search official English Kubernetes documentation."""

import argparse
import json
import subprocess
from pathlib import Path

from app.documentation.store import DocumentationStore
from app.retrieval.embeddings import IssueEmbedder


UPSTREAM = "https://github.com/kubernetes/website.git"
DEFAULT_SOURCE = Path("data/kubernetes-website")
DEFAULT_INDEX = Path("data/kubernetes_docs.sqlite")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="Fetch/update official English docs")
    fetch.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    index = commands.add_parser("index", help="Build/update the local docs index")
    index.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    index.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    search = commands.add_parser("search", help="Search the local docs index")
    search.add_argument("--query", required=True)
    search.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    search.add_argument("--limit", type=int, default=5)
    return parser


def fetch_repository(destination: Path) -> None:
    if (destination / ".git").is_dir():
        subprocess.run(
            ["git", "-C", str(destination), "pull", "--ff-only"], check=True
        )
        return
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("source destination exists and is not an empty Git checkout")
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git", "clone", "--depth=1", "--filter=blob:none", "--sparse",
            UPSTREAM, str(destination),
        ],
        check=True,
    )
    subprocess.run(
        [
            "git", "-C", str(destination), "sparse-checkout", "set",
            "content/en/docs/concepts", "content/en/docs/tasks",
            "content/en/docs/tutorials", "content/en/docs/reference",
        ],
        check=True,
    )


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "fetch":
        fetch_repository(args.source)
        output = {"source": str(args.source), "status": "updated"}
    else:
        store = DocumentationStore(args.index, IssueEmbedder())
        if args.command == "index":
            output = store.index_repository(args.source)
        else:
            output = {"results": store.search(args.query, args.limit)}
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
