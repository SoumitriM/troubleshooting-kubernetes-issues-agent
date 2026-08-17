import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict


class IssueComment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    author: str | None
    body: str | None
    created_at: str
    url: str


class LinkedPullRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int
    title: str
    state: str
    url: str


class ClosingCommit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sha: str
    url: str


class EvidenceError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: str
    error: str


class ResolutionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comments: list[IssueComment]
    linked_pull_requests: list[LinkedPullRequest]
    closing_commits: list[ClosingCommit]
    errors: list[EvidenceError]


class ResolutionEvidenceClient:
    """Fetch explicit resolution evidence from GitHub's REST API."""

    MAX_RATE_LIMIT_RETRIES = 1

    def __init__(
        self,
        token: str | None = None,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
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
        self._sleeper = sleeper
        self._clock = clock

    def __enter__(self) -> "ResolutionEvidenceClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch_evidence(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        comments_count: int,
    ) -> ResolutionEvidence:
        comments: list[IssueComment] = []
        pull_requests: list[LinkedPullRequest] = []
        commits: list[ClosingCommit] = []
        errors: list[EvidenceError] = []

        if comments_count > 0:
            try:
                comments = self._fetch_comments(owner, repo, issue_number)
            except Exception as error:
                errors.append(self._safe_error("comments", error))

        try:
            events = self._get_paginated(
                f"/repos/{owner}/{repo}/issues/{issue_number}/timeline"
            )
            pull_requests = self._extract_pull_requests(events)
            commits = self._extract_closing_commits(events)
        except Exception as error:
            errors.append(self._safe_error("timeline", error))

        return ResolutionEvidence(
            comments=comments,
            linked_pull_requests=pull_requests,
            closing_commits=commits,
            errors=errors,
        )

    def _fetch_comments(
        self, owner: str, repo: str, issue_number: int
    ) -> list[IssueComment]:
        items = self._get_paginated(
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments"
        )
        return [
            IssueComment(
                id=item["id"],
                author=(item.get("user") or {}).get("login"),
                body=item.get("body"),
                created_at=item["created_at"],
                url=item["html_url"],
            )
            for item in items
        ]

    def _get_paginated(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            response = self._get_with_rate_limit_retry(
                path,
                params={"per_page": 100, "page": page},
            )
            response.raise_for_status()
            page_items = response.json()
            if not isinstance(page_items, list):
                raise ValueError("GitHub response is not a list")
            items.extend(page_items)
            if len(page_items) < 100:
                return items
            page += 1

    def _get_with_rate_limit_retry(
        self, path: str, params: dict[str, int]
    ) -> httpx.Response:
        for attempt in range(self.MAX_RATE_LIMIT_RETRIES + 1):
            response = self._client.get(path, params=params)
            if not self._is_rate_limited(response) or attempt >= self.MAX_RATE_LIMIT_RETRIES:
                return response
            self._sleeper(self._retry_delay(response))
        raise RuntimeError("unreachable")

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        return response.status_code == 429 or (
            response.status_code == 403
            and (
                response.headers.get("X-RateLimit-Remaining") == "0"
                or "Retry-After" in response.headers
            )
        )

    def _retry_delay(self, response: httpx.Response) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return max(float(retry_after), 0.0)
            except ValueError:
                pass
        reset = response.headers.get("X-RateLimit-Reset")
        if reset is not None:
            try:
                return max(float(reset) - self._clock(), 0.0)
            except ValueError:
                pass
        return 1.0

    @staticmethod
    def _extract_pull_requests(
        events: list[dict[str, Any]],
    ) -> list[LinkedPullRequest]:
        unique: dict[int, LinkedPullRequest] = {}
        for event in events:
            source_issue = (event.get("source") or {}).get("issue") or {}
            if event.get("event") != "cross-referenced" or not source_issue.get(
                "pull_request"
            ):
                continue
            pull_request = LinkedPullRequest(
                number=source_issue["number"],
                title=source_issue["title"],
                state=source_issue["state"],
                url=source_issue["html_url"],
            )
            unique.setdefault(pull_request.number, pull_request)
        return list(unique.values())

    @staticmethod
    def _extract_closing_commits(
        events: list[dict[str, Any]],
    ) -> list[ClosingCommit]:
        unique: dict[str, ClosingCommit] = {}
        for event in events:
            sha = event.get("commit_id")
            url = event.get("commit_url")
            if event.get("event") != "closed" or not sha or not url:
                continue
            unique.setdefault(sha, ClosingCommit(sha=sha, url=url))
        return list(unique.values())

    @staticmethod
    def _safe_error(request: str, error: Exception) -> EvidenceError:
        if isinstance(error, httpx.HTTPStatusError):
            description = f"GitHub HTTP {error.response.status_code}"
        else:
            description = f"{type(error).__name__}"
        return EvidenceError(request=request, error=description)
