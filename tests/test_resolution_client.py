import httpx

from app.ingestion.resolution_client import ResolutionEvidenceClient


def comment_payload(identifier: int) -> dict[str, object]:
    return {
        "id": identifier,
        "user": {"login": "octocat"},
        "body": f"Comment {identifier}",
        "created_at": "2025-01-01T00:00:00Z",
        "html_url": f"https://github.com/example/project/issues/42#comment-{identifier}",
    }


def test_fetch_evidence_paginates_and_deduplicates_timeline_evidence() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/comments"):
            if request.url.params["page"] == "1":
                return httpx.Response(
                    200,
                    json=[comment_payload(number) for number in range(100)],
                )
            return httpx.Response(200, json=[comment_payload(100)])
        pull_request = {
            "event": "cross-referenced",
            "source": {
                "issue": {
                    "number": 7,
                    "title": "Fix collector",
                    "state": "closed",
                    "html_url": "https://github.com/example/project/pull/7",
                    "pull_request": {"url": "https://api.github.com/pulls/7"},
                }
            },
        }
        closing_commit = {
            "event": "closed",
            "commit_id": "abc123",
            "commit_url": "https://api.github.com/repos/example/project/commits/abc123",
        }
        return httpx.Response(
            200,
            json=[pull_request, pull_request, closing_commit, closing_commit],
        )

    http_client = httpx.Client(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(handler),
    )
    client = ResolutionEvidenceClient(client=http_client)

    evidence = client.fetch_evidence("example", "project", 42, comments_count=101)

    assert len(evidence.comments) == 101
    assert evidence.comments[0].model_dump() == {
        "id": 0,
        "author": "octocat",
        "body": "Comment 0",
        "created_at": "2025-01-01T00:00:00Z",
        "url": "https://github.com/example/project/issues/42#comment-0",
    }
    assert [pull.number for pull in evidence.linked_pull_requests] == [7]
    assert [commit.sha for commit in evidence.closing_commits] == ["abc123"]
    assert evidence.errors == []
    assert [request.url.params["page"] for request in requests[:2]] == ["1", "2"]
    assert requests[2].url.path.endswith("/timeline")


def test_zero_comments_skips_only_comments_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    client = ResolutionEvidenceClient(
        client=httpx.Client(
            base_url="https://api.github.com",
            transport=httpx.MockTransport(handler),
        )
    )

    evidence = client.fetch_evidence("example", "project", 42, comments_count=0)

    assert evidence.comments == []
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/timeline")


def test_request_failure_is_recorded_while_other_request_continues() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/comments"):
            return httpx.Response(500, json={"message": "failure"})
        return httpx.Response(
            200,
            json=[
                {
                    "event": "closed",
                    "commit_id": "abc123",
                    "commit_url": "https://api.github.com/commits/abc123",
                }
            ],
        )

    client = ResolutionEvidenceClient(
        client=httpx.Client(
            base_url="https://api.github.com",
            transport=httpx.MockTransport(handler),
        )
    )

    evidence = client.fetch_evidence("example", "project", 42, comments_count=1)

    assert evidence.comments == []
    assert [commit.sha for commit in evidence.closing_commits] == ["abc123"]
    assert evidence.errors[0].model_dump() == {
        "request": "comments",
        "error": "GitHub HTTP 500",
    }


def test_rate_limit_waits_and_retries_once() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                403,
                headers={"X-RateLimit-Remaining": "0", "Retry-After": "2"},
                json={"message": "rate limited"},
            )
        return httpx.Response(200, json=[])

    client = ResolutionEvidenceClient(
        client=httpx.Client(
            base_url="https://api.github.com",
            transport=httpx.MockTransport(handler),
        ),
        sleeper=delays.append,
    )

    evidence = client.fetch_evidence("example", "project", 42, comments_count=0)

    assert evidence.errors == []
    assert attempts == 2
    assert delays == [2.0]
