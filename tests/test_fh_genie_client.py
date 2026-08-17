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
    assert "Pods restart repeatedly" in request["messages"][1]["content"]
    assert "CrashLoopBackOff on 1.30" in request["messages"][1]["content"]


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
