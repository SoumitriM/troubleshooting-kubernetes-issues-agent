import argparse
import json
import logging
from pathlib import Path
from typing import Any, Iterator

from app.enrichment.claude_client import (
    EnrichmentResponseError,
    ClaudeClient,
    ClaudeSettings,
)


logger = logging.getLogger(__name__)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Enrich issues with Claude")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--limit", type=positive_int)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Discard existing output and start enrichment again",
    )
    return parser


def read_issues(input_path: Path) -> Iterator[dict[str, Any]]:
    with input_path.open("r", encoding="utf-8") as input_file:
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


def issue_key(issue: dict[str, Any], source: str) -> str:
    issue_number = issue.get("issue_number")
    if issue_number is None:
        raise ValueError(f"Issue record in {source} has no issue_number")
    return str(issue_number)


def read_existing_output(output_path: Path) -> dict[str, dict[str, Any]]:
    """Read prior records, collapsing any legacy duplicates by issue number."""

    if not output_path.exists():
        return {}

    records: dict[str, dict[str, Any]] = {}
    for record in read_issues(output_path):
        records[issue_key(record, str(output_path))] = record
    return records


def write_output(
    output_path: Path, records: dict[str, dict[str, Any]]
) -> None:
    """Atomically checkpoint one output record per issue number."""

    temporary_path = output_path.with_name(f".{output_path.name}.tmp")
    with temporary_path.open("w", encoding="utf-8") as output_file:
        for record in records.values():
            output_file.write(json.dumps(record, ensure_ascii=False) + "\n")
    temporary_path.replace(output_path)


def enrich_jsonl(
    input_path: Path,
    output_path: Path,
    client: ClaudeClient,
    limit: int | None = None,
    overwrite: bool = False,
) -> tuple[int, int]:
    """Resume enrichment and return counts newly succeeded and failed."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    issues: dict[str, dict[str, Any]] = {}
    for issue in read_issues(input_path):
        issues.setdefault(issue_key(issue, str(input_path)), issue)

    output_records = {} if overwrite else read_existing_output(output_path)
    completed = {
        key
        for key, record in output_records.items()
        if record.get("enrichment") is not None
    }
    target = limit if limit is not None else len(issues)
    succeeded = failed = 0

    # Rewrite once up front so even legacy duplicate output becomes unique.
    write_output(output_path, output_records)
    if len(completed) >= target:
        return succeeded, failed

    for key, issue in issues.items():
        if key in completed:
            continue
        if len(completed) >= target:
            break

        issue_number = issue["issue_number"]
        print(f"Enriching {len(completed) + 1}/{target}")
        try:
            enrichment = client.enrich(
                title=str(issue.get("title") or ""),
                body=issue.get("body"),
            )
            output_records[key] = {
                **issue,
                "enrichment": enrichment.model_dump(mode="json"),
            }
            completed.add(key)
            succeeded += 1
        except Exception as error:
            reason = (
                error.reason
                if isinstance(error, EnrichmentResponseError)
                else f"client error ({type(error).__name__})"
            )
            logger.error("Failed to enrich issue %s: %s", issue_number, reason)
            output_records[key] = {**issue, "enrichment": None}
            failed += 1
        write_output(output_path, output_records)

    return succeeded, failed


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = build_parser().parse_args()
    client = ClaudeClient(ClaudeSettings())
    succeeded, failed = enrich_jsonl(
        args.input,
        args.output,
        client,
        limit=args.limit,
        overwrite=args.overwrite,
    )
    print(f"Added {succeeded} enriched issues to {args.output} ({failed} failed)")


if __name__ == "__main__":
    main()
