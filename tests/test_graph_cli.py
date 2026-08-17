import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from app.graph import cli


def test_cli_loads_requested_file_without_real_database_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "issues.jsonl"
    input_path.write_text("", encoding="utf-8")
    driver = MagicMock()
    loader = MagicMock()
    loader.load_jsonl.return_value = 7

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
    monkeypatch.setattr(cli, "GraphLoader", lambda supplied_driver: loader)
    monkeypatch.setattr(
        sys,
        "argv",
        ["app.graph.cli", "--input", str(input_path)],
    )

    cli.main()

    loader.load_jsonl.assert_called_once_with(input_path)
    driver.close.assert_called_once_with()
