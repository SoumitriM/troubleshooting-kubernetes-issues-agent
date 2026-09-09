import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from app.enrichment.fh_genie_client import (
    EnrichmentResponseError,
    EnrichmentSettings,
    FHGenieClient,
)


def response(content: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def settings() -> EnrichmentSettings:
    return EnrichmentSettings(
        _env_file=None,
        fh_genie_key="test-secret",
        fh_genie_base_url="https://fh-genie.example/v1",
        fh_genie_model="test-model",
    )


def test_enrich_sends_title_and_body_and_validates_response() -> None:
    api = MagicMock()
    api.chat.completions.create.return_value = response(
        '{"summary":"Pods restart","symptom":"CrashLoopBackOff",'
        '"expected_behavior":null,"components":["kubelet"],'
        '"error_messages":["CrashLoopBackOff"],"kubernetes_versions":["1.30"]}'
    )

    result = FHGenieClient(settings(), client=api).enrich(
        "Pods restart repeatedly", "Kubelet reports CrashLoopBackOff on 1.30"
    )

    assert result.summary == "Pods restart"
    assert result.expected_behavior is None
    assert result.components == ["kubelet"]
    request = api.chat.completions.create.call_args.kwargs
    assert request["model"] == "test-model"
    assert request["temperature"] == 1.0
    assert request["max_completion_tokens"] == 2048
    assert request["extra_body"] == {"reasoning_split": True}
    issue_payload = json.loads(request["messages"][1]["content"])
    assert issue_payload == {
        "title": "Pods restart repeatedly",
        "body": "Kubelet reports CrashLoopBackOff on 1.30",
    }


def test_enrich_retries_once_after_invalid_json() -> None:
    api = MagicMock()
    api.chat.completions.create.side_effect = [
        response("not json"),
        response(
            '{"summary":null,"symptom":null,"expected_behavior":null,'
            '"components":[],"error_messages":[],"kubernetes_versions":[]}'
        ),
    ]

    result = FHGenieClient(settings(), client=api).enrich("Title", None)

    assert result.summary is None
    assert api.chat.completions.create.call_count == 2


def test_enrich_fails_after_two_invalid_responses() -> None:
    api = MagicMock()
    api.chat.completions.create.return_value = response("not json")

    with pytest.raises(EnrichmentResponseError, match="invalid JSON response"):
        FHGenieClient(settings(), client=api).enrich("Title", "Body")

    assert api.chat.completions.create.call_count == 2


def test_enrich_reports_schema_failure_without_response_content() -> None:
    api = MagicMock()
    sensitive_response = '{"summary": 123, "private": "do-not-log"}'
    api.chat.completions.create.return_value = response(sensitive_response)

    with pytest.raises(
        EnrichmentResponseError, match="response schema validation failed"
    ) as raised:
        FHGenieClient(settings(), client=api).enrich("Title", "Body")

    assert sensitive_response not in str(raised.value)


def test_api_key_is_secret_and_redacted() -> None:
    configured = settings()

    assert isinstance(configured.fh_genie_key, SecretStr)
    assert "test-secret" not in repr(configured)


def resolution_issue() -> dict[str, object]:
    return {"title": "Collector loops", "body": "Valid owners keep processing",
        "resolution_evidence": {"comments": [{"id": 1, "author": "maintainer",
            "body": "Fixed by the linked commit", "created_at": "2025-01-01T00:00:00Z",
            "url": "https://github.com/example/project/issues/1#issuecomment-1"}],
            "linked_pull_requests": [], "closing_commits": [{"sha": "abc123",
            "url": "https://api.github.com/repos/example/project/commits/abc123"}],
            "errors": []}}


def test_extract_resolution_sends_requested_evidence_and_validates() -> None:
    api = MagicMock()
    api.chat.completions.create.return_value = response(
        '{"resolution_found":true,"resolution_type":"fixed","root_cause":null,'
        '"resolution_summary":"Fixed by abc123","verified_steps":[],'
        '"evidence_urls":["https://api.github.com/repos/example/project/commits/abc123"],'
        '"confidence":"high"}')
    result = FHGenieClient(settings(), client=api).extract_resolution(resolution_issue())
    assert result.resolution_type == "fixed"
    payload = json.loads(api.chat.completions.create.call_args.kwargs["messages"][1]["content"])
    assert set(payload) == {"title", "body", "comments", "linked_pull_requests", "closing_commits"}
    system_prompt = api.chat.completions.create.call_args.kwargs["messages"][0][
        "content"
    ]
    assert "disposition_verified" in system_prompt
    assert "actionable_fix_available" in system_prompt
    assert result.disposition_verified is True
    assert result.actionable_fix_available is False


def test_extract_resolution_retries_invalid_json_once() -> None:
    api = MagicMock()
    api.chat.completions.create.side_effect = [response("not json"), response(
        '{"resolution_found":false,"resolution_type":"unknown","root_cause":null,'
        '"resolution_summary":null,"verified_steps":[],"evidence_urls":[],'
        '"confidence":"low"}')]
    result = FHGenieClient(settings(), client=api).extract_resolution(resolution_issue())
    assert result.resolution_found is False
    assert api.chat.completions.create.call_count == 2


def test_extract_resolution_rejects_invented_evidence_url() -> None:
    api = MagicMock()
    api.chat.completions.create.return_value = response(
        '{"resolution_found":true,"resolution_type":"fixed","root_cause":null,'
        '"resolution_summary":"Fixed","verified_steps":[],'
        '"evidence_urls":["https://invented.example/fix"],"confidence":"high"}')
    with pytest.raises(EnrichmentResponseError,
        match="response contains unsupported evidence URL"):
        FHGenieClient(settings(), client=api).extract_resolution(resolution_issue())
    assert api.chat.completions.create.call_count == 2


def test_generate_grounded_fix_accepts_only_supplied_steps_and_citations() -> None:
    api = MagicMock()
    evidence_url = "https://github.com/example/project/pull/1"
    api.chat.completions.create.return_value = response(
        '{"answer":"Admission policy required",'
        '"verified_steps":["Enable the admission policy"],'
        f'"citations":["{evidence_url}"]}}'
    )

    answer = FHGenieClient(settings(), client=api).generate_grounded_fix(
        "How do I fix this?",
        [
            {
                "resolution_type": "configuration",
                "resolution_summary": "Admission policy required",
                "verified_steps": ["Enable the admission policy"],
                "evidence_urls": [evidence_url],
            }
        ],
    )

    assert answer.verified_steps == ["Enable the admission policy"]
    assert answer.citations == [evidence_url]


def test_generate_grounded_fix_retries_and_rejects_invented_step() -> None:
    api = MagicMock()
    api.chat.completions.create.return_value = response(
        '{"answer":"Fixed",'
        '"verified_steps":["kubectl invented-command"],'
        '"citations":["https://github.com/example/project/pull/1"]}'
    )

    with pytest.raises(
        EnrichmentResponseError,
        match="response contains unsupported fix step",
    ):
        FHGenieClient(settings(), client=api).generate_grounded_fix(
            "How do I fix this?",
            [
                {
                    "resolution_type": "fixed",
                    "resolution_summary": "Fixed",
                    "verified_steps": ["Apply patch 1"],
                    "evidence_urls": ["https://github.com/example/project/pull/1"],
                }
            ],
        )

    assert api.chat.completions.create.call_count == 2


def test_generate_grounded_fix_rejects_invented_summary() -> None:
    api = MagicMock()
    api.chat.completions.create.return_value = response(
        '{"answer":"Invented root cause and fix",'
        '"verified_steps":["Apply patch 1"],'
        '"citations":["https://github.com/example/project/pull/1"]}'
    )

    with pytest.raises(
        EnrichmentResponseError,
        match="response contains unsupported fix summary",
    ):
        FHGenieClient(settings(), client=api).generate_grounded_fix(
            "How do I fix this?",
            [
                {
                    "resolution_type": "fixed",
                    "resolution_summary": "Verified fix summary",
                    "verified_steps": ["Apply patch 1"],
                    "evidence_urls": ["https://github.com/example/project/pull/1"],
                }
            ],
        )

    assert api.chat.completions.create.call_count == 2


def test_documentation_answer_accepts_only_retrieved_citation() -> None:
    api = MagicMock()
    source = "https://kubernetes.io/docs/concepts/overview/components/"
    api.chat.completions.create.return_value = response(
        '{"answer":"The kubelet runs on each node.",'
        f'"citations":["{source}"]}}'
    )
    answer = FHGenieClient(settings(), client=api).answer_documentation_question(
        "What does kubelet do?",
        [{"text": "The kubelet runs on each node.", "source_url": source}],
    )
    assert answer.citations == [source]


def test_documentation_answer_rejects_invented_citation() -> None:
    api = MagicMock()
    api.chat.completions.create.return_value = response(
        '{"answer":"Invented","citations":["https://example.invalid/docs"]}'
    )
    with pytest.raises(
        EnrichmentResponseError, match="unsupported citation"
    ):
        FHGenieClient(settings(), client=api).answer_documentation_question(
            "What does kubelet do?",
            [{
                "text": "The kubelet runs on each node.",
                "source_url": "https://kubernetes.io/docs/concepts/overview/components/",
            }],
        )
