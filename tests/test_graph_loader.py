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


def test_graph_password_is_redacted() -> None:
    settings = GraphSettings(
        _env_file=None,
        neo4j_username="neo4j",
        neo4j_password="test-password",
    )

    assert "test-password" not in repr(settings)
