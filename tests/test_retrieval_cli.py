import json
import logging
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from app.retrieval import cli


def configure_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    driver = MagicMock()
    store = MagicMock()
    graph_retriever = MagicMock()
    monkeypatch.setattr(
        cli,
        "GraphSettings",
        lambda: SimpleNamespace(
            neo4j_uri="bolt://localhost:7687",
            neo4j_username="neo4j",
            neo4j_password=SecretStr("test-password"),
        ),
    )
    monkeypatch.setattr(cli.GraphDatabase, "driver", lambda *_args, **_kwargs: driver)
    monkeypatch.setattr(cli, "IssueEmbedder", MagicMock)
    monkeypatch.setattr(cli, "IssueVectorStore", lambda *_args: store)
    monkeypatch.setattr(cli, "GraphRetriever", lambda *_args: graph_retriever)
    return driver, store, graph_retriever


def test_index_cli_indexes_without_real_model_or_database(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    driver, store, _graph_retriever = configure_cli(monkeypatch)
    store.index_issues.return_value = 3
    monkeypatch.setattr(sys, "argv", ["app.retrieval.cli", "index"])

    cli.main()

    assert json.loads(capsys.readouterr().out) == {"indexed_changed_issues": 3}
    driver.close.assert_called_once_with()


def test_search_cli_prints_json_results(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    driver, store, _graph_retriever = configure_cli(monkeypatch)
    store.search.return_value = [{"issue_number": 42, "score": 0.9}]
    monkeypatch.setattr(
        sys,
        "argv",
        ["app.retrieval.cli", "search", "--query", "collector", "--limit", "2"],
    )

    cli.main()

    assert json.loads(capsys.readouterr().out) == {
        "query_mode": "raw_query_only",
        "results": [{"issue_number": 42, "score": 0.9}],
    }
    store.search.assert_called_once_with("collector", 2)
    driver.close.assert_called_once_with()


def test_graph_search_cli_prints_one_context_object(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    driver, _store, graph_retriever = configure_cli(monkeypatch)
    graph_retriever.search.return_value = {
        "query": "valid owners",
        "query_mode": "raw_query_only",
        "matched_issues": [{"issue_number": 42, "score": 0.9}],
        "related_issues": [],
    }
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "app.retrieval.cli",
            "graph-search",
            "--query",
            "valid owners",
            "--limit",
            "5",
        ],
    )

    cli.main()

    output = json.loads(capsys.readouterr().out)
    assert output["matched_issues"][0]["score"] == 0.9
    assert output["query_mode"] == "raw_query_only"
    graph_retriever.search.assert_called_once_with(
        query_text="valid owners",
        query_analysis=None,
        limit=5,
        debug=False,
    )
    driver.close.assert_called_once_with()


def test_graph_search_debug_prints_relevance_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    driver, _store, graph_retriever = configure_cli(monkeypatch)
    graph_retriever.search.return_value = {
        "query": "CrashLoopBackOff",
        "query_mode": "raw_query_only",
        "matched_issues": [
            {
                "issue_number": 501,
                "relevance": {
                    "vector_similarity": 0.78,
                    "final_relevance_score": 0.846,
                },
            }
        ],
        "related_issues": [],
        "debug": {"candidate_pool_size": 20, "rejected_candidates": []},
    }
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "app.retrieval.cli",
            "graph-search",
            "--query",
            "CrashLoopBackOff",
            "--debug",
        ],
    )

    cli.main()

    output = json.loads(capsys.readouterr().out)
    assert output["matched_issues"][0]["relevance"]["vector_similarity"] == 0.78
    assert output["debug"]["candidate_pool_size"] == 20
    assert output["query_mode"] == "raw_query_only"
    assert "matching_components" not in output["matched_issues"][0]["relevance"]
    graph_retriever.search.assert_called_once_with(
        query_text="CrashLoopBackOff",
        query_analysis=None,
        limit=5,
        debug=True,
    )
    driver.close.assert_called_once_with()


def test_retrieval_cli_keeps_model_progress_off_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    driver = MagicMock()
    store = MagicMock()
    store.search.return_value = []
    monkeypatch.setattr(
        cli,
        "GraphSettings",
        lambda: SimpleNamespace(
            neo4j_uri="bolt://localhost:7687",
            neo4j_username="neo4j",
            neo4j_password=SecretStr("test-password"),
        ),
    )
    monkeypatch.setattr(cli.GraphDatabase, "driver", lambda *_args, **_kwargs: driver)

    def noisy_embedder() -> object:
        print("model-loading progress")
        logging.getLogger("huggingface_hub").warning(
            "Unauthenticated requests to the HF Hub are slower"
        )
        logging.getLogger("huggingface_hub").error("real retrieval model error")
        return object()

    monkeypatch.setattr(cli, "IssueEmbedder", noisy_embedder)
    monkeypatch.setattr(cli, "IssueVectorStore", lambda *_args: store)
    monkeypatch.setattr(
        sys,
        "argv",
        ["app.retrieval.cli", "search", "--query", "collector"],
    )

    cli.main()

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "query_mode": "raw_query_only",
        "results": [],
    }
    assert "model-loading progress" not in captured.out
    assert "model-loading progress" in captured.err
    assert "unauthenticated" not in captured.err.lower()
    assert "real retrieval model error" in caplog.text
