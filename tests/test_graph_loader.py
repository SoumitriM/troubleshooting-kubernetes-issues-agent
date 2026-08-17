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
    assert parameters["owner"] == "kubernetes"
    assert parameters["repository_name"] == "kubernetes"
    assert parameters["issue_number"] == 42
    assert parameters["labels"] == ["bug", "triage"]


def test_graph_password_is_redacted() -> None:
    settings = GraphSettings(
        _env_file=None,
        neo4j_username="neo4j",
        neo4j_password="test-password",
    )

    assert "test-password" not in repr(settings)
