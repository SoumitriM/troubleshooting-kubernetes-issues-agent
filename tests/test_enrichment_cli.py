import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.enrichment import cli
from app.enrichment.models import IssueEnrichment


class FakeClient:
    def enrich(self, title: str, body: str | None) -> IssueEnrichment:
        if title == "Broken extraction":
            raise ValueError("body text must not appear in logs")
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
    assert (succeeded, failed) == (1, 1)
    assert len(records) == 2
    assert records[0]["custom_field"] == {"preserve": True}
    assert records[0]["body"] == "Sensitive complete issue body"
    assert records[0]["enrichment"]["components"] == ["kubelet"]
    assert records[0]["enrichment"]["symptom"] is None
    assert records[1]["enrichment"] is None
    assert "Failed to enrich issue 2" in caplog.text
    assert "sensitive complete issue body" not in caplog.text.lower()


def test_cli_uses_settings_and_client_without_real_api_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "raw.jsonl"
    output_path = tmp_path / "enriched.jsonl"
    write_issues(input_path)

    monkeypatch.setattr(cli, "EnrichmentSettings", lambda: SimpleNamespace())
    monkeypatch.setattr(cli, "FHGenieClient", lambda _settings: FakeClient())
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
