# AI Incident Resolution Platform

Initial FastAPI service for an AI-assisted incident-resolution platform. It
provides a health endpoint, GitHub issue ingestion, FH Genie issue enrichment,
and a local Neo4j graph loader. It does not yet include a frontend or
Kubernetes deployment configuration.

## Prerequisites

- Python 3.11
- Docker with Docker Compose

## Local setup

Create and activate a virtual environment:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

Install the application and development dependencies:

```bash
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Optionally create a local environment file:

```bash
cp .env.example .env
```

`GITHUB_TOKEN` is optional for public repositories. Set it in `.env` to use
authenticated GitHub API rate limits. Never commit the `.env` file.

Set `NEO4J_USERNAME` and `NEO4J_PASSWORD` in `.env` before starting Neo4j.
For issue enrichment, set `FH_GENIE_KEY`, `FH_GENIE_BASE_URL`, and
`FH_GENIE_MODEL`. The key is loaded as a secret and must never be committed.

Run the API locally:

```bash
uvicorn app.main:app --reload
```

The health endpoint is available at `http://127.0.0.1:8000/health`.

Run the tests:

```bash
pytest
```

## GitHub issue ingestion

Fetch closed issues from a public repository and write normalized JSONL:

```bash
python -m app.ingestion.cli \
  --owner kubernetes \
  --repo kubernetes \
  --limit 100 \
  --output data/raw/kubernetes_issues.jsonl
```

The command creates missing output directories, follows API pagination, and
excludes pull requests returned by GitHub's Issues API. Each line contains
only the issue number, title, body, labels, state, timestamps, comment count,
and HTML URL. Generated files under `data/raw/` are ignored by Git.

## FH Genie issue enrichment

Enrich an ingested JSONL file using the configured FH Genie
OpenAI-compatible endpoint:

```bash
python -m app.enrichment.cli \
  --input data/raw/kubernetes_issues.jsonl \
  --output data/processed/kubernetes_issues_enriched.jsonl \
  --limit 10
```

For each issue, the command sends only its title and body to the configured
model. It writes a separate JSONL file and preserves every source field. The
validated `enrichment` object contains `summary`, `symptom`,
`expected_behavior`, `components`, `error_messages`, and
`kubernetes_versions`. Missing scalar values are `null`; missing collections
are empty lists.

Invalid model output is retried once. If an issue still fails, its original
record is written with `enrichment: null`, its issue number is logged, and the
remaining issues continue. Issue bodies and sensitive configuration are not
logged. Generated files under `data/processed/` are ignored by Git.

## Neo4j graph

Start Neo4j Community Edition locally:

```bash
docker compose up -d neo4j
```

Neo4j Browser is available at `http://localhost:7474`, and the Bolt endpoint
uses `bolt://localhost:7687`. Database state is retained in the named Docker
volume `neo4j_data`.

After ingesting the JSONL file, load it into the graph:

```bash
python -m app.graph.cli --input data/raw/kubernetes_issues.jsonl
```

The loader derives the repository from each issue URL and uses Cypher `MERGE`
for `Repository`, `Issue`, and `Label` nodes and their `HAS_ISSUE` and
`HAS_LABEL` relationships. Re-running it does not duplicate graph entities.

Stop the service without deleting its data:

```bash
docker compose down
```

## Project structure

```text
.
├── src/app/          # FastAPI application package
│   ├── graph/        # Neo4j graph loader and CLI
│   ├── enrichment/   # FH Genie client, models, and JSONL CLI
│   └── ingestion/    # GitHub issue ingestion client and CLI
├── tests/            # Automated tests
├── .env.example      # Local configuration template
├── docker-compose.yml # Local Neo4j Community Edition service
└── pyproject.toml    # Project metadata and dependencies
```
