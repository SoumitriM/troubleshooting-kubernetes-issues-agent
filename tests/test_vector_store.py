import hashlib
from unittest.mock import MagicMock

from app.retrieval.vector_store import IssueVectorStore


def issue_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "repository": "kubernetes/kubernetes",
        "issue_number": 42,
        "title": "Garbage collector loops",
        "summary": "Objects are repeatedly processed",
        "symptom": "Valid owners do not stop processing",
        "expected_behavior": "Collection should finish",
        "embedding_text_hash": None,
    }
    record.update(overrides)
    return record


def configured_session(records: list[dict[str, object]]) -> tuple[MagicMock, MagicMock]:
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    create_result = MagicMock()
    list_result = MagicMock()
    list_result.data.return_value = records
    update_result = MagicMock()
    session.run.side_effect = [create_result, list_result, update_result]
    return driver, session


def test_index_creates_cosine_index_and_updates_changed_issue() -> None:
    driver, session = configured_session([issue_record()])
    embedder = MagicMock()
    embedder.encode.return_value = [[0.0] * 384]

    count = IssueVectorStore(driver, embedder).index_issues()

    assert count == 1
    create_query = session.run.call_args_list[0].args[0]
    assert "issue_embedding_index" in create_query
    assert "`vector.dimensions`: 384" in create_query
    assert "`vector.similarity_function`: 'cosine'" in create_query
    searchable_text = embedder.encode.call_args.args[0][0]
    assert "Title: Garbage collector loops" in searchable_text
    assert "Summary: Objects are repeatedly processed" in searchable_text
    assert "Symptom: Valid owners do not stop processing" in searchable_text
    assert "Expected behavior: Collection should finish" in searchable_text
    update = session.run.call_args_list[2]
    assert update.kwargs["embedding"] == [0.0] * 384
    assert update.kwargs["text_hash"] == hashlib.sha256(
        searchable_text.encode("utf-8")
    ).hexdigest()


def test_index_skips_issue_when_searchable_text_is_unchanged() -> None:
    record = issue_record()
    text = IssueVectorStore.build_searchable_text(record)
    record["embedding_text_hash"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    create_result = MagicMock()
    list_result = MagicMock()
    list_result.data.return_value = [record]
    session.run.side_effect = [create_result, list_result]
    embedder = MagicMock()

    assert IssueVectorStore(driver, embedder).index_issues() == 0
    embedder.encode.assert_called_once_with([])
    assert session.run.call_count == 2


def test_search_returns_requested_fields_and_score() -> None:
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    query_result = MagicMock()
    query_result.data.return_value = [
        {
            "issue_number": 42,
            "title": "Garbage collector loops",
            "summary": "Objects repeat",
            "url": "https://github.com/kubernetes/kubernetes/issues/42",
            "score": 0.91,
        }
    ]
    session.run.return_value = query_result
    embedder = MagicMock()
    embedder.encode_query.return_value = [0.0] * 384

    results = IssueVectorStore(driver, embedder).search("valid owners", 5)

    assert results[0]["issue_number"] == 42
    assert results[0]["score"] == 0.91
    call = session.run.call_args
    assert "db.index.vector.queryNodes" in call.args[0]
    assert call.kwargs == {"embedding": [0.0] * 384, "limit": 5}
