import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from app.retrieval import cli


def configure_cli(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    driver = MagicMock()
    store = MagicMock()
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
    return driver, store


def test_index_cli_indexes_without_real_model_or_database(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    driver, store = configure_cli(monkeypatch)
    store.index_issues.return_value = 3
    monkeypatch.setattr(sys, "argv", ["app.retrieval.cli", "index"])

    cli.main()

    assert capsys.readouterr().out == "Indexed 3 changed issues\n"
    driver.close.assert_called_once_with()


def test_search_cli_prints_json_results(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    driver, store = configure_cli(monkeypatch)
    store.search.return_value = [{"issue_number": 42, "score": 0.9}]
    monkeypatch.setattr(
        sys,
        "argv",
        ["app.retrieval.cli", "search", "--query", "collector", "--limit", "2"],
    )

    cli.main()

    assert json.loads(capsys.readouterr().out) == {"issue_number": 42, "score": 0.9}
    store.search.assert_called_once_with("collector", 2)
    driver.close.assert_called_once_with()
