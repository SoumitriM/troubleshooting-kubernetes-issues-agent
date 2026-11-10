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
from app.enrichment.resolution_models import IssueResolution


logger = logging.getLogger(__name__)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract grounded issue resolutions with Claude"
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--limit", type=positive_int)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Discard existing output and start resolution extraction again",
    )
    return parser


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as input_file:
        for line_number, line in enumerate(input_file, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid JSON on line {line_number} of {path}") from error
            if not isinstance(record, dict):
                raise ValueError(f"Line {line_number} of {path} is not an object")
            yield record


def issue_key(issue: dict[str, Any], source: Path) -> str:
    if issue.get("issue_number") is None:
        raise ValueError(f"Issue record in {source} has no issue_number")
    return str(issue["issue_number"])


def read_existing_output(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    records: dict[str, dict[str, Any]] = {}
    for record in read_jsonl(path):
        resolution = record.get("resolution")
        if isinstance(resolution, dict):
            try:
                record = {
                    **record,
                    "resolution": IssueResolution.model_validate(
                        resolution
                    ).model_dump(mode="json"),
                }
            except ValueError:
                record = {**record, "resolution": None}
        records[issue_key(record, path)] = record
    return records


def write_output(path: Path, records: dict[str, dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as output_file:
        for record in records.values():
            output_file.write(json.dumps(record, ensure_ascii=False) + "\n")
    temporary.replace(path)


def extract_resolutions(
    input_path: Path,
    output_path: Path,
    client: ClaudeClient,
    limit: int | None = None,
    overwrite: bool = False,
) -> tuple[int, int]:
    """Resume extraction and return newly succeeded and failed counts."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    issues: dict[str, dict[str, Any]] = {}
    for issue in read_jsonl(input_path):
        issues.setdefault(issue_key(issue, input_path), issue)
    output_records = {} if overwrite else read_existing_output(output_path)
    completed = {
        key for key, record in output_records.items() if record.get("resolution") is not None
    }
    target = min(limit, len(issues)) if limit is not None else len(issues)
    succeeded = failed = 0

    write_output(output_path, output_records)
    for key, issue in issues.items():
        if len(completed) >= target:
            break
        if key in completed:
            continue
        issue_number = issue["issue_number"]
        print(f"Extracting resolution {len(completed) + 1}/{target}")
        try:
            resolution = client.extract_resolution(issue)
            output_records[key] = {
                **issue,
                "resolution": resolution.model_dump(mode="json"),
            }
            completed.add(key)
            succeeded += 1
        except Exception as error:
            reason = (
                error.reason
                if isinstance(error, EnrichmentResponseError)
                else f"client error ({type(error).__name__})"
            )
            logger.error(
                "Failed to extract resolution for issue %s: %s",
                issue_number,
                reason,
            )
            output_records[key] = {**issue, "resolution": None}
            failed += 1
        write_output(output_path, output_records)
    return succeeded, failed


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = build_parser().parse_args()
    client = ClaudeClient(ClaudeSettings())
    succeeded, failed = extract_resolutions(
        args.input,
        args.output,
        client,
        limit=args.limit,
        overwrite=args.overwrite,
    )
    print(f"Added {succeeded} resolutions to {args.output} ({failed} failed)")


if __name__ == "__main__":
    main()
