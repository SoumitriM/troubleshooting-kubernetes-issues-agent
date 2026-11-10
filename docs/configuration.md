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
