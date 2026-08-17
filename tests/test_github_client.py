import json
from pathlib import Path

import httpx
from pydantic import SecretStr

from app.ingestion.github_client import (
    GitHubIssueClient,
    GitHubSettings,
    save_issues_jsonl,
)


def issue_payload(number: int, *, pull_request: bool = False) -> dict[str, object]:
    issue: dict[str, object] = {
        "number": number,
        "title": f"Issue {number}",
        "body": "Example body",
        "labels": [{"name": "bug"}, {"name": "triage"}],
        "state": "closed",
        "created_at": "2025-01-01T00:00:00Z",
        "closed_at": "2025-01-02T00:00:00Z",
        "comments": 3,
        "html_url": f"https://github.com/example/project/issues/{number}",
    }
    if pull_request:
        issue["pull_request"] = {"url": "https://api.github.com/pulls/1"}
    return issue


def test_fetch_closed_issues_paginates_and_excludes_pull_requests() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = request.url.params["page"]
        if page == "1":
            items = [issue_payload(1), issue_payload(2, pull_request=True)]
            items.extend(issue_payload(number, pull_request=True) for number in range(3, 101))
            return httpx.Response(200, json=items)
        return httpx.Response(200, json=[issue_payload(101)])

    http_client = httpx.Client(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(handler),
    )
    client = GitHubIssueClient(client=http_client)

    issues = client.fetch_closed_issues("example", "project", limit=2)

    assert [issue.issue_number for issue in issues] == [1, 101]
    assert len(requests) == 2
    assert requests[0].url.params["state"] == "closed"
    assert requests[0].url.params["per_page"] == "100"


def test_save_issues_jsonl_keeps_only_normalized_fields(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        payload = issue_payload(42)
        payload["user"] = {"login": "not-persisted"}
        return httpx.Response(200, json=[payload])

    http_client = httpx.Client(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(handler),
    )
    client = GitHubIssueClient(client=http_client)
    issues = client.fetch_closed_issues("example", "project", limit=1)
    output = tmp_path / "issues.jsonl"

    save_issues_jsonl(issues, output)

    record = json.loads(output.read_text(encoding="utf-8"))
    assert record == {
        "issue_number": 42,
        "title": "Issue 42",
        "body": "Example body",
        "labels": ["bug", "triage"],
        "state": "closed",
        "created_at": "2025-01-01T00:00:00Z",
        "closed_at": "2025-01-02T00:00:00Z",
        "comments_count": 3,
        "html_url": "https://github.com/example/project/issues/42",
    }


def test_github_token_is_optional_and_redacted() -> None:
    settings = GitHubSettings(_env_file=None, github_token="test-token")

    assert isinstance(settings.github_token, SecretStr)
    assert "test-token" not in repr(settings)
