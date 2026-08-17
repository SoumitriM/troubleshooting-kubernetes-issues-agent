import argparse
import json
import logging
from pathlib import Path
from typing import Any, Iterator

from app.enrichment.fh_genie_client import EnrichmentSettings, FHGenieClient


logger = logging.getLogger(__name__)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Enrich issues with FH Genie")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--limit", type=positive_int)
    return parser


def read_issues(input_path: Path, limit: int | None) -> Iterator[dict[str, Any]]:
    with input_path.open("r", encoding="utf-8") as input_file:
        yielded = 0
        for line_number, line in enumerate(input_file, start=1):
            if not line.strip():
                continue
            try:
                issue = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid JSON on input line {line_number}") from error
            if not isinstance(issue, dict):
                raise ValueError(f"Input line {line_number} is not a JSON object")
            yield issue
            yielded += 1
            if limit is not None and yielded >= limit:
                return


def enrich_jsonl(
    input_path: Path,
    output_path: Path,
    client: FHGenieClient,
    limit: int | None = None,
) -> tuple[int, int]:
    """Write source records plus nested enrichment and return success/failure counts."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    succeeded = failed = 0
    with output_path.open("w", encoding="utf-8") as output_file:
        for issue in read_issues(input_path, limit):
            issue_number = issue.get("issue_number", "unknown")
            try:
                enrichment = client.enrich(
                    title=str(issue.get("title") or ""),
                    body=issue.get("body"),
                )
                enriched_record = {
                    **issue,
                    "enrichment": enrichment.model_dump(mode="json"),
                }
                succeeded += 1
            except Exception:
                logger.error("Failed to enrich issue %s", issue_number)
                enriched_record = {**issue, "enrichment": None}
                failed += 1
            output_file.write(json.dumps(enriched_record, ensure_ascii=False) + "\n")

    return succeeded, failed


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = build_parser().parse_args()
    settings = EnrichmentSettings()
    client = FHGenieClient(settings)
    succeeded, failed = enrich_jsonl(args.input, args.output, client, args.limit)
    print(f"Saved {succeeded + failed} issues to {args.output} ({failed} failed)")


if __name__ == "__main__":
    main()
