import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.ingestion import resolution_cli
from app.ingestion.resolution_client import ResolutionEvidence


def issue_record(number: int, comments_count: int = 1) -> dict[str, object]:
    return {
        "issue_number": number,
        "title": f"Issue {number}",
        "body": "Original body",
        "labels": ["bug"],
        "state": "closed",
        "created_at": "2025-01-01T00:00:00Z",
        "closed_at": "2025-01-02T00:00:00Z",
        "comments_count": comments_count,
        "html_url": f"https://github.com/kubernetes/kubernetes/issues/{number}",
        "enrichment": {"summary": "Preserved summary"},
    }


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def empty_evidence() -> ResolutionEvidence:
    return ResolutionEvidence(
        comments=[],
        linked_pull_requests=[],
        closing_commits=[],
        errors=[],
    )


def test_pipeline_preserves_fields_resumes_and_uses_limit_as_total(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    records = [issue_record(1), issue_record(2), issue_record(3)]
    write_jsonl(input_path, records)
    completed = {**records[0], "resolution_evidence": empty_evidence().model_dump()}
    write_jsonl(output_path, [completed, completed])
    client = MagicMock()
    client.fetch_evidence.return_value = empty_evidence()

    processed = resolution_cli.ingest_resolution_evidence(
        input_path,
        output_path,
        client,
        limit=2,
    )

    assert processed == 1
    output = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert [record["issue_number"] for record in output] == [1, 2]
    assert output[1]["enrichment"] == {"summary": "Preserved summary"}
    assert output[1]["body"] == "Original body"
    client.fetch_evidence.assert_called_once_with("kubernetes", "kubernetes", 2, 1)


def test_pipeline_records_issue_failure_and_continues(tmp_path: Path) -> None:
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    write_jsonl(input_path, [issue_record(1), issue_record(2)])
    client = MagicMock()
    client.fetch_evidence.side_effect = [RuntimeError("sensitive detail"), empty_evidence()]

    processed = resolution_cli.ingest_resolution_evidence(
        input_path,
        output_path,
        client,
    )

    assert processed == 2
    output = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert output[0]["resolution_evidence"]["errors"] == [
        {"request": "issue", "error": "RuntimeError"}
    ]
    assert output[1]["resolution_evidence"]["errors"] == []


def test_cli_uses_optional_token_without_network_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    write_jsonl(input_path, [issue_record(1)])
    fake_client = MagicMock()
    fake_client.fetch_evidence.return_value = empty_evidence()
    context_manager = MagicMock()
    context_manager.__enter__.return_value = fake_client
    monkeypatch.setattr(
        resolution_cli,
        "GitHubSettings",
        lambda: SimpleNamespace(github_token=None),
    )
    client_factory = MagicMock(return_value=context_manager)
    monkeypatch.setattr(resolution_cli, "ResolutionEvidenceClient", client_factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "app.ingestion.resolution_cli",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--limit",
            "1",
        ],
    )

    resolution_cli.main()

    client_factory.assert_called_once_with(token=None)
    fake_client.fetch_evidence.assert_called_once()
