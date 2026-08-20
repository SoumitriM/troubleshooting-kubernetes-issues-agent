import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from app.agent import cli


def test_agent_cli_uses_mocked_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    driver = MagicMock()
    workflow = MagicMock()
    workflow.invoke.return_value = {
        "route": "generate_findings",
        "answer": "No verified actionable fix was found.",
        "citations": ["https://github.com/kubernetes/kubernetes/issues/1"],
    }
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
    monkeypatch.setattr(cli, "EnrichmentSettings", lambda: object())
    monkeypatch.setattr(cli, "FHGenieClient", lambda _settings: object())
    monkeypatch.setattr(cli, "IssueEmbedder", lambda: object())
    monkeypatch.setattr(cli, "GraphRetriever", lambda *_args: object())
    monkeypatch.setattr(cli, "AgentSettings", lambda: object())
    monkeypatch.setattr(cli, "IncidentNodes", lambda *_args: object())
    monkeypatch.setattr(cli, "build_workflow", lambda _nodes: workflow)
    monkeypatch.setattr(
        sys,
        "argv",
        ["app.agent.cli", "--query", "DRA adminAccess can be enabled"],
    )

    cli.main()

    output = json.loads(capsys.readouterr().out)
    assert output["route"] == "generate_findings"
    workflow.invoke.assert_called_once_with(
        {"user_query": "DRA adminAccess can be enabled"}
    )
    driver.close.assert_called_once_with()
