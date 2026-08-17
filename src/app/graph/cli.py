import argparse
from pathlib import Path

from neo4j import GraphDatabase

from app.graph.loader import GraphLoader, GraphSettings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Load GitHub issues into Neo4j")
    parser.add_argument("--input", required=True, type=Path)
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
        loaded = GraphLoader(driver).load_jsonl(args.input)
    finally:
        driver.close()

    print(f"Loaded {loaded} issues into Neo4j")


if __name__ == "__main__":
    main()
