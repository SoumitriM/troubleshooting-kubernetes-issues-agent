import argparse
import json
import logging
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from app.ingestion.github_client import GitHubSettings
from app.ingestion.resolution_client import ResolutionEvidenceClient


logger = logging.getLogger(__name__)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch GitHub resolution evidence for issues"
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--limit", type=positive_int)
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


def repository_from_url(url: str) -> tuple[str, str]:
    parts = urlparse(url).path.strip("/").split("/")
    if len(parts) < 4 or parts[2] != "issues":
        raise ValueError("issue has an invalid GitHub URL")
    return parts[0], parts[1]


def read_existing_output(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    records: dict[str, dict[str, Any]] = {}
    for record in read_jsonl(path):
        records[issue_key(record, path)] = record
    return records


def write_output(path: Path, records: dict[str, dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as output_file:
        for record in records.values():
            output_file.write(json.dumps(record, ensure_ascii=False) + "\n")
    temporary.replace(path)


def ingest_resolution_evidence(
    input_path: Path,
    output_path: Path,
    client: ResolutionEvidenceClient,
    limit: int | None = None,
) -> int:
    """Resume evidence ingestion and return the number newly processed."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    issues: dict[str, dict[str, Any]] = {}
    for issue in read_jsonl(input_path):
        issues.setdefault(issue_key(issue, input_path), issue)
    output_records = read_existing_output(output_path)
    completed = {
        key
        for key, record in output_records.items()
        if "resolution_evidence" in record
    }
    target = min(limit, len(issues)) if limit is not None else len(issues)
    processed = 0

    write_output(output_path, output_records)
    for key, issue in issues.items():
        if len(completed) >= target:
            break
        if key in completed:
            continue
        issue_number = issue["issue_number"]
        print(f"Fetching evidence {len(completed) + 1}/{target}")
        try:
            owner, repo = repository_from_url(str(issue.get("html_url") or ""))
            evidence = client.fetch_evidence(
                owner,
                repo,
                issue_number,
                int(issue.get("comments_count") or 0),
            )
            output_records[key] = {
                **issue,
                "resolution_evidence": evidence.model_dump(mode="json"),
            }
        except Exception as error:
            logger.error(
                "Failed to fetch evidence for issue %s: %s",
                issue_number,
                type(error).__name__,
            )
            output_records[key] = {
                **issue,
                "resolution_evidence": {
                    "comments": [],
                    "linked_pull_requests": [],
                    "closing_commits": [],
                    "errors": [
                        {"request": "issue", "error": type(error).__name__}
                    ],
                },
            }
        completed.add(key)
        processed += 1
        write_output(output_path, output_records)
    return processed


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = build_parser().parse_args()
    settings = GitHubSettings()
    token = (
        settings.github_token.get_secret_value() if settings.github_token else None
    )
    with ResolutionEvidenceClient(token=token) as client:
        processed = ingest_resolution_evidence(
            args.input,
            args.output,
            client,
            limit=args.limit,
        )
    print(f"Added resolution evidence for {processed} issues to {args.output}")


if __name__ == "__main__":
    main()
