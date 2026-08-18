import json
from pathlib import Path
from unittest.mock import MagicMock

from app.graph.loader import GraphLoader, GraphSettings


def issue_record(number: int = 42) -> dict[str, object]:
    return {
        "issue_number": number,
        "title": "Example issue",
        "body": "Example body",
        "labels": ["bug", "triage"],
        "state": "closed",
        "created_at": "2025-01-01T00:00:00Z",
        "closed_at": "2025-01-02T00:00:00Z",
        "comments_count": 3,
        "html_url": f"https://github.com/kubernetes/kubernetes/issues/{number}",
    }


def test_load_jsonl_uses_idempotent_merge_statements(tmp_path: Path) -> None:
    input_path = tmp_path / "issues.jsonl"
    input_path.write_text(json.dumps(issue_record()) + "\n", encoding="utf-8")

    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    transaction = MagicMock()
    session.execute_write.side_effect = (
        lambda callback, issue: callback(transaction, issue)
    )

    loaded = GraphLoader(driver).load_jsonl(input_path)

    assert loaded == 1
    query = transaction.run.call_args.args[0]
    parameters = transaction.run.call_args.kwargs
    assert "MERGE (repository:Repository" in query
    assert "MERGE (issue:Issue" in query
    assert "MERGE (label:Label" in query
    assert "MERGE (repository)-[:HAS_ISSUE]->(issue)" in query
    assert "MERGE (issue)-[:HAS_LABEL]->(label)" in query
    assert "MERGE (component:Component" in query
    assert "MERGE (version:KubernetesVersion" in query
    assert "MERGE (error:ErrorMessage" in query
    assert parameters["owner"] == "kubernetes"
    assert parameters["repository_name"] == "kubernetes"
    assert parameters["issue_number"] == 42
    assert parameters["labels"] == ["bug", "triage"]
    assert parameters["has_enrichment"] is False
    assert parameters["summary"] is None
    assert parameters["components"] == []


def test_load_enriched_issue_merges_normalized_graph_entities(tmp_path: Path) -> None:
    record = issue_record()
    record["enrichment"] = {
        "summary": "Pods fail to start",
        "symptom": "Pods remain pending",
        "expected_behavior": "Pods should become ready",
        "components": [" Kubelet ", "kubelet", "Scheduler", "  "],
        "error_messages": ["Failed to create pod", "Failed to create pod", ""],
        "kubernetes_versions": ["v1.30.1", "v1.30.1", "v1.31.0"],
    }
    input_path = tmp_path / "enriched.jsonl"
    input_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    transaction = MagicMock()
    session.execute_write.side_effect = (
        lambda callback, issue: callback(transaction, issue)
    )

    assert GraphLoader(driver).load_jsonl(input_path) == 1

    query = transaction.run.call_args.args[0]
    parameters = transaction.run.call_args.kwargs
    assert "MERGE (issue)-[:AFFECTS_COMPONENT]->(component)" in query
    assert "MERGE (issue)-[:REPORTED_IN_VERSION]->(version)" in query
    assert "MERGE (issue)-[:HAS_ERROR]->(error)" in query
    assert "DELETE" not in query.upper()
    assert "DETACH" not in query.upper()
    assert parameters["has_enrichment"] is True
    assert parameters["summary"] == "Pods fail to start"
    assert parameters["symptom"] == "Pods remain pending"
    assert parameters["expected_behavior"] == "Pods should become ready"
    assert parameters["components"] == ["kubelet", "scheduler"]
    assert parameters["error_messages"] == ["Failed to create pod"]
    assert parameters["kubernetes_versions"] == ["v1.30.1", "v1.31.0"]


def test_load_verified_resolution_merges_evidence_graph(tmp_path: Path) -> None:
    record = issue_record()
    comment_url = "https://github.com/kubernetes/kubernetes/issues/42#issuecomment-1"
    uncited_comment_url = (
        "https://github.com/kubernetes/kubernetes/issues/42#issuecomment-2"
    )
    pull_url = "https://github.com/kubernetes/kubernetes/pull/43"
    uncited_pull_url = "https://github.com/kubernetes/kubernetes/pull/44"
    commit_url = "https://api.github.com/repos/kubernetes/kubernetes/commits/abc123"
    external_url = "https://github.com/kubernetes/kubernetes/issues/99"
    record["resolution_evidence"] = {
        "comments": [
            {
                "id": 1,
                "author": "maintainer",
                "body": "Fixed by the linked pull request",
                "created_at": "2025-01-03T00:00:00Z",
                "url": comment_url,
            },
            {
                "id": 2,
                "author": "reporter",
                "body": "Thanks",
                "created_at": "2025-01-04T00:00:00Z",
                "url": uncited_comment_url,
            },
        ],
        "linked_pull_requests": [
            {"number": 43, "title": "Fix issue", "state": "closed", "url": pull_url},
            {
                "number": 44,
                "title": "Related work",
                "state": "open",
                "url": uncited_pull_url,
            },
        ],
        "closing_commits": [{"sha": "abc123", "url": commit_url}],
        "errors": [],
    }
    record["resolution"] = {
        "resolution_found": True,
        "resolution_type": "fixed",
        "root_cause": "Stale owner reference",
        "resolution_summary": "Fixed by pull request 43",
        "verified_steps": ["Apply the owner-reference fix"],
        "evidence_urls": [comment_url, pull_url, commit_url, external_url],
        "confidence": "high",
    }
    input_path = tmp_path / "resolved.jsonl"
    input_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    transaction = MagicMock()
    session.execute_write.side_effect = (
        lambda callback, issue: callback(transaction, issue)
    )

    assert GraphLoader(driver).load_jsonl(input_path) == 1

    query = transaction.run.call_args.args[0]
    parameters = transaction.run.call_args.kwargs
    assert "MERGE (resolution:Resolution" in query
    assert "MERGE (issue)-[:HAS_RESOLUTION]->(resolution)" in query
    assert "MERGE (pull_request:PullRequest {url:" in query
    assert "MERGE (commit:Commit {sha:" in query
    assert "MERGE (comment:Comment {id:" in query
    assert "MERGE (source:EvidenceSource {url:" in query
    assert "MERGE (resolution)-[:SUPPORTED_BY]->" in query
    assert "DELETE" not in query.upper()
    assert "DETACH" not in query.upper()
    assert parameters["has_resolution"] is True
    assert parameters["resolution_type"] == "fixed"
    assert parameters["verified_steps"] == ["Apply the owner-reference fix"]
    assert [item["url"] for item in parameters["pull_requests"]] == [
        pull_url,
        uncited_pull_url,
    ]
    assert parameters["supported_pull_request_urls"] == [pull_url]
    assert parameters["supported_commit_shas"] == ["abc123"]
    assert [item["id"] for item in parameters["evidence_comments"]] == [1]
    assert parameters["unmatched_evidence_urls"] == [external_url]


def test_resolution_found_false_does_not_create_resolution_evidence(tmp_path: Path) -> None:
    record = issue_record()
    record["resolution_evidence"] = {
        "comments": [],
        "linked_pull_requests": [
            {
                "number": 43,
                "title": "Possibly related",
                "state": "open",
                "url": "https://github.com/kubernetes/kubernetes/pull/43",
            }
        ],
        "closing_commits": [],
        "errors": [],
    }
    record["resolution"] = {
        "resolution_found": False,
        "resolution_type": "unknown",
        "root_cause": None,
        "resolution_summary": None,
        "verified_steps": [],
        "evidence_urls": [],
        "confidence": "low",
    }
    input_path = tmp_path / "unresolved.jsonl"
    input_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    transaction = MagicMock()
    session.execute_write.side_effect = (
        lambda callback, issue: callback(transaction, issue)
    )

    GraphLoader(driver).load_jsonl(input_path)

    parameters = transaction.run.call_args.kwargs
    assert parameters["has_resolution"] is False
    assert parameters["pull_requests"] == []
    assert parameters["closing_commits"] == []
    assert parameters["evidence_comments"] == []
    assert parameters["unmatched_evidence_urls"] == []


def test_graph_password_is_redacted() -> None:
    settings = GraphSettings(
        _env_file=None,
        neo4j_username="neo4j",
        neo4j_password="test-password",
    )

    assert "test-password" not in repr(settings)
