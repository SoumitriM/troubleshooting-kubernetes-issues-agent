# Local development

This guide runs the whole stack on macOS without Docker: Neo4j from Homebrew,
the API with uvicorn, and the UI with Vite. To use Docker Compose or
Kubernetes instead, see the [README](../README.md#docker-compose).

## Prerequisites

- Python 3.11
- Node.js 20 or newer (for the UI and Claude Code)
- Homebrew
- Claude access, through either:
  - an Anthropic API key, or
  - Claude Code 2.1.280 or newer, signed in (`claude --version` to check)

## 1. Install dependencies

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
(cd frontend && npm ci)
```

## 2. Start Neo4j

```bash
brew install neo4j
neo4j-admin dbms set-initial-password '<password>'   # first run only
neo4j start
```

Set `NEO4J_USERNAME=neo4j` and `NEO4J_PASSWORD` in `.env`. Neo4j Browser is at
http://localhost:7474 and Bolt at `bolt://localhost:7687`. Stop it with
`neo4j stop`; the data is kept.

If the graph is empty, load the processed issues and build the vector index:

```bash
python -m app.graph.cli --input data/processed/kubernetes_issues_with_resolutions.jsonl
python -m app.retrieval.cli index
```

## 3. Build the documentation index

Skip this if `data/kubernetes_docs.sqlite` already exists.

```bash
python -m app.documentation.cli fetch
python -m app.documentation.cli index
```

## 4. Choose how the app reaches Claude

Pick one in `.env`. See [LLM integration](llm-integration.md) for how each
backend works.

**Anthropic API**, the default and the only option in containers:

```bash
LLM_BACKEND=api
ANTHROPIC_API_KEY=<your key>
```

**Claude Code login**, for local development without an API key:

```bash
LLM_BACKEND=claude_code
LLM_CLAUDE_CODE_PATH=claude
```

Each model call runs `claude -p`, so it uses your Claude subscription and
counts against its usage limits. If the API is started from a shell where
`claude` is not on `PATH` (for example, without nvm loaded), set
`LLM_CLAUDE_CODE_PATH` to the full path printed by `which claude`. Update an
old CLI with `npm i -g @anthropic-ai/claude-code@latest`.

## 5. Start the API and UI

In one terminal:

```bash
source .venv/bin/activate
env -u CLAUDE_EFFORT uvicorn app.main:app --port 8000 --reload
```

`env -u CLAUDE_EFFORT` only matters when the command is launched from inside a
Claude Code session, which exports that variable. Settings use the `LLM_`
prefix, so it is harmless otherwise.

In a second terminal:

```bash
cd frontend
npm run dev
```

Open http://127.0.0.1:5173. Vite proxies `/api`, `/health` and `/ready` to the
API on port 8000.

## 6. Check that everything works

```bash
curl -s localhost:8000/ready
curl -s -X POST localhost:8000/api/knowledge/query \
  -H 'content-type: application/json' \
  -d '{"query":"What does the kubelet do?"}'
```

`/ready` returns `{"status":"ready"}` once the workflow is built. It also
reports ready while Neo4j is still starting; graph routes connect once Neo4j
is reachable.

| Symptom | Likely cause |
|---|---|
| Every answer says "query-understanding service is temporarily unavailable" | Claude cannot be reached. Check the API log for the reason: no API key, Claude Code not signed in or too old, or a timeout |
| `/ready` returns 503 | Startup failed. The API log shows the exception |
| Documentation questions find nothing | `data/kubernetes_docs.sqlite` is missing or `DOCUMENTATION_INDEX_PATH` is wrong |
| Graph questions return no rows | Neo4j is empty or not running |

## Tests

```bash
python -m pytest -q        # backend, no external services needed
(cd frontend && npm test)  # UI API client
```

The backend tests replace Claude, Neo4j and the embedder with fakes, so they
are fast and free. To test against the real services, use the
[golden evaluation](evaluation.md).
