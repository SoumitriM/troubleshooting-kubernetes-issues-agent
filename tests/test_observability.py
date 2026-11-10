import os

import pytest

from app.observability import TracingSettings, configure_tracing
from tests.test_claude_client import client, response


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith("LANGSMITH_"):
            monkeypatch.delenv(name)
    yield


def settings(**values) -> TracingSettings:
    return TracingSettings(_env_file=None, **values)


def test_tracing_is_off_by_default() -> None:
    assert configure_tracing(settings()) is False
    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert "LANGSMITH_API_KEY" not in os.environ


def test_tracing_without_an_api_key_stays_off() -> None:
    assert configure_tracing(settings(langsmith_tracing=True)) is False
    assert os.environ["LANGSMITH_TRACING"] == "false"


def test_enabled_tracing_exports_settings() -> None:
    enabled = configure_tracing(
        settings(
            langsmith_tracing=True,
            langsmith_api_key="lsv2-test",
            langsmith_project="kubesage-dev",
            langsmith_tracing_sampling_rate=0.2,
            langsmith_hide_inputs=True,
        )
    )

    assert enabled is True
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == "lsv2-test"
    assert os.environ["LANGSMITH_PROJECT"] == "kubesage-dev"
    assert os.environ["LANGSMITH_TRACING_SAMPLING_RATE"] == "0.2"
    assert os.environ["LANGSMITH_HIDE_INPUTS"] == "true"
    assert os.environ["LANGSMITH_HIDE_OUTPUTS"] == "false"


def test_api_key_is_never_shown() -> None:
    assert "lsv2-test" not in repr(settings(langsmith_api_key="lsv2-test"))


def test_traced_model_call_returns_the_same_result_with_tracing_off() -> None:
    os.environ["LANGSMITH_TRACING"] = "false"
    claude, api = client(response(
        '{"summary":null,"symptom":null,"expected_behavior":null,'
        '"components":["kubelet"],"error_messages":[],"kubernetes_versions":[]}'
    ))

    assert claude.enrich("Title", None).components == ["kubelet"]
    # The tracing wrapper does not leak its extra argument into the request.
    assert "langsmith_extra" not in api.beta.messages.create.call_args.kwargs
