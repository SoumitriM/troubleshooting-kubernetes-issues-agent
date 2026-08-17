import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

from app.ingestion import cli
from app.ingestion.github_client import GitHubIssue


def test_cli_writes_issues_without_network_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "nested" / "issues.jsonl"

    class FakeGitHubIssueClient:
        def __init__(self, token: str | None = None) -> None:
            self.token = token

        def __enter__(self) -> "FakeGitHubIssueClient":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def fetch_closed_issues(
            self, owner: str, repo: str, limit: int
        ) -> list[GitHubIssue]:
            assert (owner, repo, limit) == ("kubernetes", "kubernetes", 100)
            return [
                GitHubIssue(
                    issue_number=1,
                    title="Test issue",
                    body=None,
                    labels=[],
                    state="closed",
                    created_at=datetime.fromisoformat("2025-01-01T00:00:00+00:00"),
                    closed_at=datetime.fromisoformat("2025-01-02T00:00:00+00:00"),
                    comments_count=0,
                    html_url="https://github.com/kubernetes/kubernetes/issues/1",
                )
            ]

    monkeypatch.setattr(cli, "GitHubIssueClient", FakeGitHubIssueClient)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "app.ingestion.cli",
            "--owner",
            "kubernetes",
            "--repo",
            "kubernetes",
            "--limit",
            "100",
            "--output",
            str(output),
        ],
    )

    cli.main()

    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["issue_number"] == 1
