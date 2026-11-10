import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.enrichment import cli
from app.enrichment.claude_client import EnrichmentResponseError
from app.enrichment.models import IssueEnrichment


class FakeClient:
    def enrich(self, title: str, body: str | None) -> IssueEnrichment:
        if title == "Broken extraction":
            raise EnrichmentResponseError("invalid JSON response")
        return IssueEnrichment(
            summary="Supported summary",
            components=["kubelet"],
        )


def write_issues(path: Path) -> None:
    records = [
        {
            "issue_number": 1,
            "title": "Working extraction",
            "body": "Sensitive complete issue body",
            "labels": ["bug"],
            "custom_field": {"preserve": True},
        },
        {
            "issue_number": 2,
            "title": "Broken extraction",
            "body": "Another sensitive complete issue body",
            "labels": [],
        },
        {"issue_number": 3, "title": "Beyond limit", "body": None},
    ]
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )


def test_enrich_jsonl_preserves_fields_and_continues_after_failure(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    input_path = tmp_path / "raw.jsonl"
    output_path = tmp_path / "processed" / "enriched.jsonl"
    write_issues(input_path)

    with caplog.at_level(logging.ERROR):
        succeeded, failed = cli.enrich_jsonl(
            input_path, output_path, FakeClient(), limit=2
        )

    records = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert (succeeded, failed) == (2, 1)
    assert len(records) == 3
    assert records[0]["custom_field"] == {"preserve": True}
    assert records[0]["body"] == "Sensitive complete issue body"
    assert records[0]["enrichment"]["components"] == ["kubelet"]
    assert records[0]["enrichment"]["symptom"] is None
    assert records[1]["enrichment"] is None
    assert "Failed to enrich issue 2: invalid JSON response" in caplog.text
    assert "sensitive complete issue body" not in caplog.text.lower()


def test_resume_skips_completed_issues_and_targets_total(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "raw.jsonl"
    output_path = tmp_path / "enriched.jsonl"
    write_issues(input_path)
    first_issue = json.loads(input_path.read_text().splitlines()[0])
    first_issue["enrichment"] = IssueEnrichment(
        summary="Already complete"
    ).model_dump(mode="json")
    # Include a legacy duplicate to verify that resume collapses it.
    output_path.write_text(
        json.dumps(first_issue) + "\n" + json.dumps(first_issue) + "\n",
        encoding="utf-8",
    )

    class TrackingClient:
        def __init__(self) -> None:
            self.titles: list[str] = []

        def enrich(self, title: str, body: str | None) -> IssueEnrichment:
            self.titles.append(title)
            return IssueEnrichment(summary=title)

    client = TrackingClient()
    succeeded, failed = cli.enrich_jsonl(
        input_path, output_path, client, limit=3
    )

    records = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert (succeeded, failed) == (2, 0)
    assert client.titles == ["Broken extraction", "Beyond limit"]
    assert [record["issue_number"] for record in records] == [1, 2, 3]
    assert len({record["issue_number"] for record in records}) == 3
    progress = capsys.readouterr().out
    assert "Enriching 2/3" in progress
    assert "Enriching 3/3" in progress


def test_failed_record_is_retried_without_duplication(tmp_path: Path) -> None:
    input_path = tmp_path / "raw.jsonl"
    output_path = tmp_path / "enriched.jsonl"
    write_issues(input_path)
    failed_issue = json.loads(input_path.read_text().splitlines()[0])
    failed_issue["enrichment"] = None
    output_path.write_text(json.dumps(failed_issue) + "\n", encoding="utf-8")

    succeeded, failed = cli.enrich_jsonl(
        input_path, output_path, FakeClient(), limit=1
    )

    records = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert (succeeded, failed) == (1, 0)
    assert len(records) == 1
    assert records[0]["issue_number"] == 1
    assert records[0]["enrichment"] is not None


def test_overwrite_starts_again_and_discards_prior_output(tmp_path: Path) -> None:
    input_path = tmp_path / "raw.jsonl"
    output_path = tmp_path / "enriched.jsonl"
    write_issues(input_path)
    output_path.write_text(
        json.dumps({"issue_number": 999, "enrichment": {"summary": "old"}})
        + "\n",
        encoding="utf-8",
    )

    succeeded, failed = cli.enrich_jsonl(
        input_path, output_path, FakeClient(), limit=1, overwrite=True
    )

    records = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert (succeeded, failed) == (1, 0)
    assert [record["issue_number"] for record in records] == [1]


def test_cli_uses_settings_and_client_without_real_api_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "raw.jsonl"
    output_path = tmp_path / "enriched.jsonl"
    write_issues(input_path)

    monkeypatch.setattr(cli, "ClaudeSettings", lambda: SimpleNamespace())
    monkeypatch.setattr(cli, "ClaudeClient", lambda _settings: FakeClient())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "app.enrichment.cli",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--limit",
            "1",
        ],
    )

    cli.main()

    assert len(output_path.read_text(encoding="utf-8").splitlines()) == 1
