# LLM integration

All model calls go through `ClaudeClient` in
[`src/app/enrichment/claude_client.py`](../src/app/enrichment/claude_client.py).
System prompts are in [`prompts.py`](../src/app/enrichment/prompts.py).

## What Claude is used for

| Method | Called by | Output model |
|---|---|---|
| `analyze_query` | `understand_query`, once per question and per clarification answer | `QueryUnderstanding` |
| `generate_cypher` | Analytics questions without a matching template | `CypherGeneration` |
| `answer_documentation_question` | Documentation route | `GroundedDocumentationAnswer` |
| `verify_incident_matches` | Incident route, after reranking | `IncidentMatchVerification` |
| `research_step` | Research route, once per step (at most five) | `ResearchStep` |
| `generate_grounded_fix` | Incident route, when a verified fix exists | `GroundedFixAnswer` |
| `enrich` | `app.enrichment.cli` | `IssueEnrichment` |
| `extract_resolution` | `app.enrichment.resolution_cli` | `IssueResolution` |
| `normalize_query`, `classify_query` | Nothing; superseded by `analyze_query` | `NormalizedQuery`, `IntentClassification` |

Graph analytics answers do not call Claude:
[`formatting.py`](../src/app/graph/formatting.py) renders the query rows, so
they contain only database results.

A typical question makes one to three calls: `analyze_query`, then an answer
call on the documentation and fix routes, plus `verify_incident_matches` on
the incident route.

## Request flow

Every method goes through `_structured`:

1. The input is serialised to JSON and sent as the user message, with the
   method's system prompt.
2. The output is constrained to a JSON schema built from the Pydantic model by
   `anthropic.transform_schema`.
3. The reply is validated against the full Pydantic model. Structured outputs
   do not enforce numeric or length bounds, so these are checked here.
4. A method-specific evidence check runs (below).
5. If any step fails, the request is retried once, with the reason appended to
   the system prompt. A second failure raises `EnrichmentResponseError`
   containing only the reason, never the model's text.

A refusal (`stop_reason == "refusal"`) is raised immediately without a retry.
A truncated reply (`max_tokens`) counts as a failure and is retried.

The workflow catches these errors. A failed `analyze_query` produces the
"temporarily unavailable" answer. A failed answer call falls back to
deterministic output: the best documentation excerpt, or the verified fix
exactly as stored.

## Evidence checks

| Method | The reply is rejected if |
|---|---|
| `generate_grounded_fix` | The summary is not one of the supplied resolution summaries, or any step or citation was not supplied |
| `answer_documentation_question` | A citation is not the source URL of a retrieved chunk |
| `extract_resolution` | An evidence URL does not appear in the issue's comments, PRs or commits |
| `generate_cypher` | Parameter names repeat. The Cypher itself is then checked by `CypherValidator` |
| `verify_incident_matches` | An issue number is missing, repeated, or was not supplied |
| `research_step` | A final answer cites a URL no tool returned, a tool call lacks its arguments, or a tool is requested after the four-call budget |

Structured outputs require every object to forbid extra keys, so a free-form
mapping cannot be expressed. `generate_cypher` therefore asks for parameters as
a list of `{"name", "value"}` pairs and converts them to a dictionary.

## Backends

`LLM_BACKEND` selects how a request reaches Claude. Everything above is
identical for both.

### `api` (default)

Uses the Anthropic Python SDK:

```python
client.beta.messages.create(
    model=LLM_MODEL,
    max_tokens=16000,
    system=system_prompt,
    messages=[{"role": "user", "content": payload_json}],
    output_config={"effort": LLM_EFFORT, "format": {"type": "json_schema", "schema": ...}},
    betas=["server-side-fallback-2026-07-01"],  # when LLM_REFUSAL_FALLBACK=true
    fallbacks="default",
)
```

Adaptive thinking is always on for this model, so no `thinking` or
`temperature` parameters are sent. `LLM_EFFORT` controls how much it thinks.

### `claude_code`

Runs each request through the Claude Code CLI with the user's existing login,
so no API key is needed:

```bash
claude --print --output-format json --model "$LLM_MODEL" --effort "$LLM_EFFORT" \
  --system-prompt "<prompt>" --json-schema '<schema>' \
  --tools "" --strict-mcp-config --disable-slash-commands \
  --setting-sources "" --no-session-persistence
```

The payload is passed on stdin. The flags remove tools, MCP servers, skills,
local settings and saved sessions, so each call is an isolated completion. The
validated JSON is read from the result's `structured_output` field.
A CLI error (not signed in, version too old, usage limit) raises
`RuntimeError` with the CLI's message and is not retried.

Use it for local development and evaluation only:

- It bills the signed-in user's Claude plan.
- Each call starts a new process, adding about 2 s.
- Refusal fallback and SDK retries do not apply.
- Container images do not include the CLI.

## Changing prompts or the model

- Run `python -m pytest tests/test_claude_client.py` after changing the client;
  these tests check request shape, retries and evidence checks without network
  access.
- Run the [golden evaluation](evaluation.md) after changing a prompt, the
  model or `LLM_EFFORT`. Routing depends on `QUERY_UNDERSTANDING_SYSTEM_PROMPT`,
  so small wording changes can move borderline queries between routes.
- Keep the instruction to treat user and issue text as data in every prompt.
