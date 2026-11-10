# Configuration

All settings come from environment variables, with `.env` in the working
directory as a fallback. Names are case-insensitive. Start from
[`.env.example`](../.env.example) and never commit `.env`.

Secrets (`ANTHROPIC_API_KEY`, `NEO4J_PASSWORD`, `GITHUB_TOKEN`) are loaded as
Pydantic `SecretStr` values, so they are hidden in reprs and logs.

## Claude

Defined in `ClaudeSettings` ([`claude_client.py`](../src/app/enrichment/claude_client.py)).
Used by the API and by the enrichment and resolution CLIs.

| Variable | Default | Description |
|---|---|---|
| `LLM_BACKEND` | `api` | `api` calls the Anthropic API. `claude_code` runs each call through headless Claude Code with its existing login, for local development only |
| `ANTHROPIC_API_KEY` | none | API key for the `api` backend. If unset, the SDK falls back to `ANTHROPIC_AUTH_TOKEN` or an `ant auth login` profile |
| `LLM_CLAUDE_CODE_PATH` | `claude` | Claude Code executable for the `claude_code` backend. Needs version 2.1.280 or newer for `claude-opus-5-5` |
| `LLM_MODEL` | `claude-opus-5-5` | Model ID for every call |
| `LLM_EFFORT` | `low` | Thinking effort: `low`, `medium`, `high`, `xhigh` or `max`. Raise it if answer quality drops |
| `LLM_TIMEOUT_SECONDS` | `60` | Per-request timeout, also applied to each Claude Code process |
| `LLM_MAX_RETRIES` | `1` | SDK retries for transient API errors (0–5); `api` backend only |
| `LLM_REFUSAL_FALLBACK` | `true` | Re-run a request that Claude declines on safety grounds on a fallback model, in the same call; `api` backend only |

These use an `LLM_` prefix rather than `CLAUDE_` because Claude Code exports
`CLAUDE_*` variables (such as `CLAUDE_EFFORT`) to the processes it starts,
which would silently override them.

## LangSmith tracing

Defined in `TracingSettings` ([`observability.py`](../src/app/observability.py)).
Off by default. When enabled, the API exports these settings at startup, so
values in `.env` work even though pydantic settings do not export them.
LangSmith's free single-user plan includes a monthly trace allowance; check
its current limits. Each chat turn produces one trace.

| Variable | Default | Description |
|---|---|---|
| `LANGSMITH_TRACING` | `false` | Trace each LangGraph run and every Claude call. Needs `LANGSMITH_API_KEY`; without it tracing stays off |
| `LANGSMITH_API_KEY` | none | Key from smith.langchain.com. Secret: never commit it |
| `LANGSMITH_PROJECT` | `kubesage` | Project the traces are grouped under |
| `LANGSMITH_TRACING_SAMPLING_RATE` | `1.0` | Fraction of runs traced (0–1); lower it to stay within the free allowance |
| `LANGSMITH_HIDE_INPUTS` / `LANGSMITH_HIDE_OUTPUTS` | `false` | Keep user messages, prompts, issue text and answers out of LangSmith, sending only structure, timings and metadata |
| `LANGSMITH_ENDPOINT` | LangSmith default | Only for another region or a self-hosted LangSmith |

A trace shows every workflow node with its state changes. Each Claude call
appears as an `llm` run named after its output model (for example
`QueryUnderstanding` or `ComposedReply`), with the system prompt, the JSON
payload, the output and metadata for the model, effort, backend and attempt
number, so a rejected response and its retry appear as two runs.

Traces send user messages, prompts and retrieved issue text to LangSmith's
cloud. Enable tracing only for development and evaluation, or set both HIDE
variables, in deployments where that data must stay inside the cluster.

## Neo4j

Defined in `GraphSettings` ([`loader.py`](../src/app/graph/loader.py)).

| Variable | Default | Description |
|---|---|---|
| `NEO4J_URI` | `bolt://localhost:7687` | Bolt endpoint |
| `NEO4J_USERNAME` | required | Database user |
| `NEO4J_PASSWORD` | required | Database password |

Docker Compose and Kubernetes also read `NEO4J_AUTH` (`user/password`) to
initialise the Neo4j container.

## Agent routing

Defined in `AgentSettings` ([`nodes.py`](../src/app/agent/nodes.py)). Values are
between 0 and 1.

| Variable | Default | Description |
|---|---|---|
| `AGENT_RELEVANCE_THRESHOLD` | `0.55` | Minimum top similarity score for incident findings |
| `AGENT_ACTIONABLE_FIX_THRESHOLD` | `0.65` | Minimum top similarity score before a verified fix is offered |
| `DOCUMENTATION_INDEX_PATH` | `data/kubernetes_docs.sqlite` | Documentation index built by `app.documentation.cli index` |

## Incident retrieval

Defined in `GraphRetrieverSettings` ([`graph_retriever.py`](../src/app/retrieval/graph_retriever.py)).
Values are between 0 and 1.

| Variable | Default | Description |
|---|---|---|
| `RETRIEVAL_MIN_VECTOR_SCORE` | `0.55` | Candidates below this vector similarity are discarded |
| `RETRIEVAL_EXCEPTIONAL_VECTOR_SCORE` | `0.90` | Above this score, a candidate may be accepted on similarity alone |
| `RETRIEVAL_MIN_FINAL_RELEVANCE_SCORE` | `0.55` | Minimum score after reranking |
| `RETRIEVAL_MIN_RELATIVE_SCORE_RATIO` | `0.85` | Drops matches scoring below this fraction of the best match |

## GitHub ingestion

| Variable | Default | Description |
|---|---|---|
| `GITHUB_TOKEN` | none | Optional; raises GitHub API rate limits for the ingestion CLIs |

## Fixed paths

| Path | Contents |
|---|---|
| `data/checkpoints.sqlite` | Clarification thread checkpoints, created at API startup |
| `data/conversations.sqlite` | Chat conversations and messages, created at API startup |
| `data/raw/`, `data/processed/` | Pipeline outputs (ignored by Git) |
| `data/kubernetes-website/` | Sparse checkout of the Kubernetes docs source |

## Containers

The Docker image and the Kubernetes manifests use the `api` backend.

- Kubernetes: `ANTHROPIC_API_KEY` and the Neo4j credentials come from the
  `incident-ai-secrets` Secret, and `LLM_MODEL` and `LLM_EFFORT` from
  [`k8s/01-configmap.yaml`](../k8s/01-configmap.yaml).
- Docker Compose: the same values are injected from a runtime env file. See the
  [README](../README.md#docker-compose).
