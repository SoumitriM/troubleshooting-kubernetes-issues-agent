import json
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class GitHubSettings(BaseSettings):
    """Configuration loaded from environment variables and an optional .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    github_token: SecretStr | None = None


class GitHubIssue(BaseModel):
    """The normalized subset of a GitHub issue used by the platform."""

    model_config = ConfigDict(extra="ignore")

    issue_number: int
    title: str
    body: str | None
    labels: list[str]
    state: str
    created_at: datetime
    closed_at: datetime | None
    comments_count: int
    html_url: str


class GitHubIssueClient:
    """Fetch closed issues from GitHub's REST API."""

    def __init__(
        self,
        token: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url="https://api.github.com",
            headers=headers,
            timeout=30.0,
        )

    def __enter__(self) -> "GitHubIssueClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch_closed_issues(
        self,
        owner: str,
        repo: str,
        limit: int,
    ) -> list[GitHubIssue]:
        if limit < 1:
            raise ValueError("limit must be at least 1")

        issues: list[GitHubIssue] = []
        page = 1
        per_page = 100

        while len(issues) < limit:
            response = self._client.get(
                f"/repos/{owner}/{repo}/issues",
                params={
                    "state": "closed",
                    "per_page": per_page,
                    "page": page,
                },
            )
            response.raise_for_status()
            items: list[dict[str, Any]] = response.json()

            for item in items:
                if "pull_request" in item:
                    continue
                issues.append(self._normalize_issue(item))
                if len(issues) == limit:
                    break

            if len(items) < per_page:
                break
            page += 1

        return issues

    @staticmethod
    def _normalize_issue(item: dict[str, Any]) -> GitHubIssue:
        return GitHubIssue(
            issue_number=item["number"],
            title=item["title"],
            body=item.get("body"),
            labels=[label["name"] for label in item.get("labels", [])],
            state=item["state"],
            created_at=item["created_at"],
            closed_at=item.get("closed_at"),
            comments_count=item["comments"],
            html_url=item["html_url"],
        )


def save_issues_jsonl(issues: list[GitHubIssue], output: Path) -> None:
    """Write normalized issues as one JSON object per line."""

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as output_file:
        for issue in issues:
            record = issue.model_dump(mode="json")
            output_file.write(json.dumps(record, ensure_ascii=False) + "\n")
