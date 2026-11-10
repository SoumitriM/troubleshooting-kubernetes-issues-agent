import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.agent.state import NormalizedQuery
from app.enrichment.claude_client import (
    REFUSAL_FALLBACK_BETA,
    ClaudeClient,
    ClaudeSettings,
    EnrichmentResponseError,
)


def response(text: str, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
    )


def settings(**overrides) -> ClaudeSettings:
    return ClaudeSettings(_env_file=None, anthropic_api_key="test-secret", **overrides)


def client(*responses: SimpleNamespace, **overrides) -> tuple[ClaudeClient, MagicMock]:
    api = MagicMock()
    api.beta.messages.create.side_effect = list(responses)
    return ClaudeClient(settings(**overrides), client=api), api


EMPTY_ENRICHMENT = (
    '{"summary":null,"symptom":null,"expected_behavior":null,'
    '"components":[],"error_messages":[],"kubernetes_versions":[]}'
)


def test_enrich_sends_schema_constrained_request_and_validates() -> None:
    claude, api = client(response(
        '{"summary":"Pods restart","symptom":"CrashLoopBackOff",'
        '"expected_behavior":null,"components":["kubelet"],'
        '"error_messages":["CrashLoopBackOff"],"kubernetes_versions":["1.30"]}'
    ))

    result = claude.enrich(
        "Pods restart repeatedly", "Kubelet reports CrashLoopBackOff on 1.30"
    )

    assert result.components == ["kubelet"]
    request = api.beta.messages.create.call_args.kwargs
    assert request["model"] == "claude-opus-5-5"
    assert request["output_config"]["effort"] == "low"
    schema = request["output_config"]["format"]
    assert schema["type"] == "json_schema"
    assert schema["schema"]["additionalProperties"] is False
    assert request["betas"] == [REFUSAL_FALLBACK_BETA]
    assert request["fallbacks"] == "default"
    assert "temperature" not in request and "thinking" not in request
    assert json.loads(request["messages"][0]["content"]) == {
        "title": "Pods restart repeatedly",
        "body": "Kubelet reports CrashLoopBackOff on 1.30",
    }


def test_refusal_fallback_can_be_disabled() -> None:
    claude, api = client(response(EMPTY_ENRICHMENT), llm_refusal_fallback=False)

    claude.enrich("Title", None)

    request = api.beta.messages.create.call_args.kwargs
    assert "fallbacks" not in request and "betas" not in request


def test_invalid_response_is_retried_once_with_feedback() -> None:
    claude, api = client(response("not json"), response(EMPTY_ENRICHMENT))

    result = claude.enrich("Title", None)

    assert result.summary is None
    assert api.beta.messages.create.call_count == 2
    retry_system = api.beta.messages.create.call_args_list[1].kwargs["system"]
    assert "previous response was rejected" in retry_system


def test_two_invalid_responses_raise_without_content() -> None:
    claude, api = client(response("not json"), response("still not json"))

    with pytest.raises(EnrichmentResponseError, match="invalid JSON response"):
        claude.enrich("Title", "Body")

    assert api.beta.messages.create.call_count == 2


def test_refusal_is_not_retried() -> None:
    claude, api = client(response("", stop_reason="refusal"))

    with pytest.raises(EnrichmentResponseError, match="declined"):
        claude.analyze_query("query")

    assert api.beta.messages.create.call_count == 1


def test_truncated_responses_fail() -> None:
    claude, _api = client(
        response("{", stop_reason="max_tokens"), response("{", stop_reason="max_tokens")
    )

    with pytest.raises(EnrichmentResponseError, match="truncated"):
        claude.analyze_query("query")


def test_api_key_is_secret_and_redacted() -> None:
    configured = settings()

    assert configured.anthropic_api_key.get_secret_value() == "test-secret"
    assert "test-secret" not in repr(configured)


def test_analyze_query_returns_validated_understanding() -> None:
    claude, _api = client(response(json.dumps({
        "normalized_query": "What does the kubelet do?",
        "intent": "GENERAL_KUBERNETES_QUESTION",
        "intent_confidence": 0.98,
        "components": ["kubelet"],
        "versions": [], "entities": [], "symptoms": [], "error_messages": [],
        "observed_behavior": None,
        "has_concrete_incident": False,
        "reasoning": "Conceptual question",
        "analytics_operation": None, "group_by": None, "limit": None,
    })))

    result = claude.analyze_query("What does the kubelet do?")

    assert result.intent == "GENERAL_KUBERNETES_QUESTION"
    assert result.components == ["kubelet"]


def test_client_side_constraints_are_enforced() -> None:
    # Structured outputs drop numeric bounds from the wire schema; Pydantic
    # still enforces them locally.
    out_of_range = json.dumps({
        "normalized_query": "q", "intent": "GRAPH_ANALYTICS",
        "intent_confidence": 7.5, "reasoning": "r",
    })
    claude, _api = client(response(out_of_range), response(out_of_range))

    with pytest.raises(EnrichmentResponseError, match="schema validation failed"):
        claude.analyze_query("q")


def test_generate_cypher_converts_parameter_list_to_mapping() -> None:
    claude, api = client(response(json.dumps({
        "cypher": "MATCH (c:Component) WHERE c.name = $component RETURN c LIMIT 5",
        "parameters": [
            {"name": "component", "value": "kubelet"},
            {"name": "names", "value": ["a", "b"]},
        ],
        "explanation": "Find a component",
    })))

    generated = claude.generate_cypher(
        NormalizedQuery(normalized_query="kubelet"), intent="GRAPH_ANALYTICS"
    )

    assert generated.parameters == {"component": "kubelet", "names": ["a", "b"]}
    schema = api.beta.messages.create.call_args.kwargs["output_config"]["format"]["schema"]
    assert schema["properties"]["parameters"]["type"] == "array"


def test_generate_cypher_rejects_duplicate_parameter_names() -> None:
    duplicate = json.dumps({
        "cypher": "MATCH (i:Issue) RETURN i LIMIT 5",
        "parameters": [{"name": "x", "value": 1}, {"name": "x", "value": 2}],
        "explanation": "e",
    })
    claude, _api = client(response(duplicate), response(duplicate))

    with pytest.raises(EnrichmentResponseError, match="unique"):
        claude.generate_cypher(NormalizedQuery(normalized_query="q"), intent="GRAPH_ANALYTICS")


RESOLUTION = {
    "resolution_found": True, "disposition_verified": True,
    "actionable_fix_available": True, "resolution_type": "fixed",
    "root_cause": None, "resolution_summary": "Fixed by PR",
    "verified_steps": ["Upgrade"], "confidence": "high",
}
ISSUE = {
    "title": "Bug",
    "body": "Body",
    "resolution_evidence": {
        "comments": [{"url": "https://github.com/k/k/issues/1#issuecomment-1", "body": "fixed"}],
        "linked_pull_requests": [{"url": "https://github.com/k/k/pull/2"}],
        "closing_commits": [],
    },
}


def test_extract_resolution_accepts_supplied_evidence() -> None:
    claude, api = client(response(json.dumps(
        {**RESOLUTION, "evidence_urls": ["https://github.com/k/k/pull/2"]}
    )))

    result = claude.extract_resolution(ISSUE)

    assert result.evidence_urls == ["https://github.com/k/k/pull/2"]
    payload = json.loads(api.beta.messages.create.call_args.kwargs["messages"][0]["content"])
    assert payload["linked_pull_requests"] == [{"url": "https://github.com/k/k/pull/2"}]


def test_extract_resolution_rejects_invented_evidence_url() -> None:
    invented = response(json.dumps(
        {**RESOLUTION, "evidence_urls": ["https://example.com/invented"]}
    ))
    claude, _api = client(invented, invented)

    with pytest.raises(EnrichmentResponseError, match="unsupported evidence URL"):
        claude.extract_resolution(ISSUE)


FIXES = [{
    "resolution_summary": "Authorization corrected",
    "verified_steps": ["Enable the admission policy"],
    "evidence_urls": ["https://github.com/k/k/pull/1"],
}]


def test_grounded_fix_accepts_only_supplied_content() -> None:
    claude, _api = client(response(json.dumps({
        "answer": "Authorization corrected",
        "verified_steps": ["Enable the admission policy"],
        "citations": ["https://github.com/k/k/pull/1"],
    })))

    answer = claude.generate_grounded_fix("query", FIXES)

    assert answer.verified_steps == ["Enable the admission policy"]


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("answer", "Invented summary", "unsupported fix summary"),
        ("verified_steps", ["rm -rf /"], "unsupported fix step"),
        ("citations", ["https://example.com/x"], "unsupported citation"),
    ],
)
def test_grounded_fix_rejects_invented_content(field: str, value, reason: str) -> None:
    body = {
        "answer": "Authorization corrected",
        "verified_steps": ["Enable the admission policy"],
        "citations": ["https://github.com/k/k/pull/1"],
        field: value,
    }
    claude, _api = client(response(json.dumps(body)), response(json.dumps(body)))

    with pytest.raises(EnrichmentResponseError, match=reason):
        claude.generate_grounded_fix("query", FIXES)


CHUNKS = [{"source_url": "https://kubernetes.io/docs/a/", "text": "The kubelet runs pods."}]


def test_documentation_answer_accepts_only_retrieved_citation() -> None:
    claude, _api = client(response(json.dumps({
        "answer": "The kubelet runs pods.", "citations": ["https://kubernetes.io/docs/a/"],
    })))

    answer = claude.answer_documentation_question("What does the kubelet do?", CHUNKS)

    assert answer.citations == ["https://kubernetes.io/docs/a/"]


def test_documentation_answer_rejects_invented_citation() -> None:
    invented = response(json.dumps({
        "answer": "x", "citations": ["https://example.com/not-retrieved"],
    }))
    claude, _api = client(invented, invented)

    with pytest.raises(EnrichmentResponseError, match="unsupported citation"):
        claude.answer_documentation_question("q", CHUNKS)


def test_claude_code_backend_runs_isolated_structured_call(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "is_error": False, "stop_reason": "end_turn", "result": "",
                "structured_output": json.loads(EMPTY_ENRICHMENT),
            }),
        )

    monkeypatch.setattr("app.enrichment.claude_client.subprocess.run", fake_run)
    claude = ClaudeClient(
        settings(llm_backend="claude_code", llm_claude_code_path="/bin/claude"),
        client=MagicMock(),
    )

    result = claude.enrich("Title", None)

    assert result.summary is None
    command, kwargs = calls[0]
    assert command[0] == "/bin/claude"
    assert command[command.index("--model") + 1] == "claude-opus-5-5"
    assert command[command.index("--tools") + 1] == ""
    assert json.loads(command[command.index("--json-schema") + 1])[
        "additionalProperties"
    ] is False
    assert json.loads(kwargs["input"]) == {"title": "Title", "body": None}


def test_claude_code_backend_error_is_raised_without_retry(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=1, stdout=json.dumps({"is_error": True, "result": "Not logged in"})
        )

    monkeypatch.setattr("app.enrichment.claude_client.subprocess.run", fake_run)
    claude = ClaudeClient(settings(llm_backend="claude_code"), client=MagicMock())

    with pytest.raises(RuntimeError, match="Not logged in"):
        claude.analyze_query("query")

    assert len(calls) == 1


def test_analyze_query_sends_conversation_history_only_when_present() -> None:
    understanding = json.dumps({
        "normalized_query": "q", "intent": "GRAPH_ANALYTICS",
        "intent_confidence": 0.9, "reasoning": "r",
    })
    claude, api = client(response(understanding), response(understanding))
    history = [{"role": "user", "content": "Top kubelet issues in v1.35?"}]

    claude.analyze_query("What about v1.36?", history=history)
    claude.analyze_query("What about v1.36?")

    with_history, without = (
        json.loads(call.kwargs["messages"][0]["content"])
        for call in api.beta.messages.create.call_args_list
    )
    assert with_history["conversation_history"] == history
    assert "conversation_history" not in without


def test_research_step_rejects_unobserved_citations_and_extra_tool_calls() -> None:
    invented = response(json.dumps({
        "action": "final_answer", "reason": "done", "answer": "a",
        "citations": ["https://example.com/invented"],
    }))
    claude, _api = client(invented, invented)
    with pytest.raises(EnrichmentResponseError, match="unsupported citation"):
        claude.research_step("q", [], remaining_tool_calls=2, allowed_urls={"https://kubernetes.io/docs/a/"})

    tool = response(json.dumps({"action": "search_docs", "reason": "more", "query": "x"}))
    claude, _api = client(tool, tool)
    with pytest.raises(EnrichmentResponseError, match="no tool calls remain"):
        claude.research_step("q", [], remaining_tool_calls=0, allowed_urls=set())
