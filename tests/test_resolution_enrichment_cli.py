import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.enrichment import resolution_cli
from app.enrichment.fh_genie_client import EnrichmentResponseError
from app.enrichment.resolution_models import IssueResolution


def issue_record(number: int) -> dict[str, object]:
    return {"issue_number": number, "title": f"Issue {number}",
        "body": "Original body", "labels": ["bug"],
        "enrichment": {"summary": "Preserved summary"},
        "resolution_evidence": {"comments": [], "linked_pull_requests": [],
            "closing_commits": [], "errors": []}}


def unresolved() -> IssueResolution:
    return IssueResolution(resolution_found=False, resolution_type="unknown",
        root_cause=None, resolution_summary=None, verified_steps=[],
        evidence_urls=[], confidence="low")


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8")


def test_resolution_extraction_preserves_fields_and_resumes_without_duplicates(tmp_path: Path) -> None:
    input_path, output_path = tmp_path / "input.jsonl", tmp_path / "output.jsonl"
    issues = [issue_record(1), issue_record(2), issue_record(3)]
    write_jsonl(input_path, issues)
    legacy_resolution = unresolved().model_dump()
    legacy_resolution.pop("disposition_verified")
    legacy_resolution.pop("actionable_fix_available")
    completed = {**issues[0], "resolution": legacy_resolution}
    write_jsonl(output_path, [completed, completed])
    client = MagicMock(); client.extract_resolution.return_value = unresolved()
    assert resolution_cli.extract_resolutions(input_path, output_path, client, limit=2) == (1, 0)
    output = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert [record["issue_number"] for record in output] == [1, 2]
    assert output[1]["body"] == "Original body"
    assert output[1]["enrichment"] == {"summary": "Preserved summary"}
    assert "resolution_evidence" in output[1]
    assert output[0]["resolution"]["disposition_verified"] is False
    assert output[0]["resolution"]["actionable_fix_available"] is False
    client.extract_resolution.assert_called_once_with(issues[1])


def test_failed_record_is_retried_and_does_not_stop_later_issues(tmp_path: Path) -> None:
    input_path, output_path = tmp_path / "input.jsonl", tmp_path / "output.jsonl"
    issues = [issue_record(1), issue_record(2)]; write_jsonl(input_path, issues)
    client = MagicMock(); client.extract_resolution.side_effect = [
        EnrichmentResponseError("invalid JSON response"), unresolved()]
    assert resolution_cli.extract_resolutions(input_path, output_path, client, limit=1) == (1, 1)
    records = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert records[0]["resolution"] is None
    assert records[1]["resolution"]["resolution_found"] is False
    retry_client = MagicMock(); retry_client.extract_resolution.return_value = unresolved()
    assert resolution_cli.extract_resolutions(input_path, output_path, retry_client, limit=2) == (1, 0)
    assert len(output_path.read_text().splitlines()) == 2


def test_resolution_cli_uses_existing_fh_genie_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    input_path, output_path = tmp_path / "input.jsonl", tmp_path / "output.jsonl"
    write_jsonl(input_path, [issue_record(1)])
    settings = SimpleNamespace(); client = MagicMock(); client.extract_resolution.return_value = unresolved()
    client_factory = MagicMock(return_value=client)
    monkeypatch.setattr(resolution_cli, "EnrichmentSettings", lambda: settings)
    monkeypatch.setattr(resolution_cli, "FHGenieClient", client_factory)
    monkeypatch.setattr(sys, "argv", ["app.enrichment.resolution_cli", "--input",
        str(input_path), "--output", str(output_path), "--limit", "1"])
    resolution_cli.main()
    client_factory.assert_called_once_with(settings)
    client.extract_resolution.assert_called_once()
