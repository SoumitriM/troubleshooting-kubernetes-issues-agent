import json
import logging
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
    assert set(output) == {"route", "answer", "citations"}
    workflow.invoke.assert_called_once_with(
        {"user_query": "DRA adminAccess can be enabled"}
    )
    driver.close.assert_called_once_with()


def test_agent_cli_debug_streams_sanitized_execution_trace(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    driver = MagicMock()
    workflow = MagicMock()
    workflow.stream.return_value = [
        {
            "analyze_incident": {
                "extracted_symptom": "Pods enter CrashLoopBackOff",
                "extracted_components": ["kubelet"],
                "kubernetes_version": "v1.35",
                "error_messages": ["CrashLoopBackOff"],
                "has_sufficient_details": True,
            }
        },
        {
            "retrieve_evidence": {
                "top_similarity_score": 0.81,
                "retrieved_context": {
                    "matched_issues": [
                        {
                            "issue_number": 501,
                            "score": 0.81,
                            "relevance": {"final_relevance_score": 0.88},
                        }
                    ],
                    "related_issues": [
                        {
                            "issue_number": 502,
                            "shared_components": ["kubelet", "runtime"],
                            "shared_versions": [],
                        }
                    ],
                    "debug": {
                        "vector_candidates": [
                            {"issue_number": 501, "vector_score": 0.81},
                            {"issue_number": 999, "vector_score": 0.79},
                        ],
                        "reranking": [
                            {
                                "issue_number": 501,
                                "matching_components": ["kubelet"],
                                "matching_versions": ["v1.35"],
                                "matching_error_terms": ["crashloopbackoff"],
                                    "matching_symptom_terms": [],
                                    "conflicting_versions": [],
                                    "version_match_type": "exact",
                                "version_conflict": False,
                                "final_relevance_score": 0.88,
                                "accepted": True,
                                "rejection_reasons": [],
                            },
                            {
                                "issue_number": 999,
                                "matching_components": ["kubelet"],
                                "matching_versions": [],
                                "matching_error_terms": [],
                                    "matching_symptom_terms": [],
                                    "conflicting_versions": ["v1.34"],
                                    "version_match_type": "conflict",
                                "version_conflict": True,
                                "final_relevance_score": 0.51,
                                "accepted": False,
                                "rejection_reasons": ["critical_error_mismatch"],
                            },
                        ],
                    },
                },
            }
        },
        {
            "assess_evidence": {
                "route": "generate_findings",
                "route_reason": "relevant incidents found without a verified actionable fix",
                "actionable_fix_available": False,
            }
        },
        {
            "generate_findings": {
                "answer": "No verified actionable fix was found.",
                "citations": [
                    "https://github.com/kubernetes/kubernetes/issues/501"
                ],
            }
        },
    ]
    monkeypatch.setattr(
        cli,
        "GraphSettings",
        lambda: SimpleNamespace(
            neo4j_uri="bolt://localhost:7687",
            neo4j_username="neo4j",
            neo4j_password=SecretStr("must-not-appear"),
        ),
    )
    monkeypatch.setattr(cli.GraphDatabase, "driver", lambda *_args, **_kwargs: driver)
    monkeypatch.setattr(cli, "EnrichmentSettings", lambda: object())
    monkeypatch.setattr(cli, "FHGenieClient", lambda _settings: object())
    def noisy_embedder() -> object:
        print("model progress must be on stderr")
        logging.getLogger("huggingface_hub").warning(
            "Unauthenticated requests to the HF Hub are slower"
        )
        logging.getLogger("huggingface_hub").error("genuine model error diagnostic")
        return object()

    monkeypatch.setattr(cli, "IssueEmbedder", noisy_embedder)
    monkeypatch.setattr(cli, "GraphRetriever", lambda *_args: object())
    monkeypatch.setattr(cli, "AgentSettings", lambda: object())
    monkeypatch.setattr(cli, "IncidentNodes", lambda *_args: object())
    monkeypatch.setattr(cli, "build_workflow", lambda _nodes: workflow)
    query = "Pods enter CrashLoopBackOff after the kubelet restarts on Kubernetes v1.35"
    monkeypatch.setattr(sys, "argv", ["app.agent.cli", "--query", query, "--debug"])

    cli.main()

    captured = capsys.readouterr()
    rendered = captured.out
    output = json.loads(rendered)
    assert output["route"] == "generate_findings"
    assert output["debug"]["original_user_query"] == query
    assert output["debug"]["executed_nodes"] == [
        "analyze_incident",
        "retrieve_evidence",
        "assess_evidence",
        "generate_findings",
    ]
    assert output["debug"]["analyze_incident"] == {
        "symptom": "Pods enter CrashLoopBackOff",
        "components": ["kubelet"],
        "kubernetes_version": "v1.35",
        "error_messages": ["CrashLoopBackOff"],
        "has_sufficient_details": True,
    }
    assert output["debug"]["vector_candidates_before_reranking"][1] == {
        "issue_number": 999,
        "vector_score": 0.79,
    }
    assert output["debug"]["reranking_details"][1]["accepted"] is False
    assert output["debug"]["reranking_details"][1]["version_conflict"] is True
    assert output["debug"]["reranking_details"][1]["version_match_type"] == "conflict"
    assert output["debug"]["reranking_details"][1]["conflicting_versions"] == [
        "v1.34"
    ]
    assert (
        output["debug"]["reranking_details"][1]["rejection_reason"]
        == "critical_error_mismatch"
    )
    assert output["debug"]["issues_after_reranking"] == [
        {"issue_number": 501, "vector_score": 0.81, "final_score": 0.88}
    ]
    assert output["debug"]["graph_expanded_related_issues"][0][
        "issue_number"
    ] == 502
    assert output["debug"]["evidence_assessment"]["decision"] == "generate_findings"
    assert output["debug"]["route_reason"].startswith("relevant incidents")
    assert "must-not-appear" not in rendered
    assert "api_key" not in rendered.lower()
    assert "model progress" not in rendered
    assert "model progress must be on stderr" in captured.err
    assert "unauthenticated requests" not in captured.err.lower()
    assert "genuine model error diagnostic" in caplog.text
    workflow.stream.assert_called_once_with(
        {"user_query": query, "debug": True}, stream_mode="updates"
    )
    workflow.invoke.assert_not_called()
    driver.close.assert_called_once_with()
