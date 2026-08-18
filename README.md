# AI Incident Resolution Platform

Initial FastAPI service for an AI-assisted incident-resolution platform. It
provides a health endpoint, GitHub issue ingestion, FH Genie issue enrichment,
a local Neo4j graph loader, and semantic issue search. It does not yet include
a frontend or Kubernetes deployment configuration.

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

The command is resumable. If the output already exists, successfully enriched
issue numbers are skipped and no duplicate output records are written.
`--limit` is the target total number of successful enrichments, so rerunning
with `--limit 100` continues from (for example) `Enriching 11/100`. Failed
records are retried on a later run. To discard the existing output and start
again explicitly, add `--overwrite`.

## Resolution evidence ingestion

After enrichment, fetch explicit resolution evidence from GitHub:

```bash
python -m app.ingestion.resolution_cli \
  --input data/processed/kubernetes_issues_enriched.jsonl \
  --output data/processed/kubernetes_issues_with_evidence.jsonl \
  --limit 20
```

The command preserves every input field and adds `resolution_evidence` with
normalized comments, cross-referenced pull requests, closing commits, and any
request-specific errors. Issues reporting zero comments skip only the comments
endpoint; their timeline is still fetched. GitHub pagination and rate-limit
retry headers are handled, and the optional `GITHUB_TOKEN` is loaded from the
existing application settings without being printed.

The output is checkpointed after each issue and is resumable. Existing issue
numbers with resolution evidence are skipped, legacy duplicates are collapsed,
and `--limit` specifies the target total number of processed issues. This stage
does not call an LLM or infer whether an issue was resolved.

## Resolution extraction

Extract a grounded resolution from the collected GitHub evidence using the
existing FH Genie configuration:

```bash
python -m app.enrichment.resolution_cli \
  --input data/processed/kubernetes_issues_with_evidence.jsonl \
  --output data/processed/kubernetes_issues_with_resolutions.jsonl \
  --limit 20
```

The command analyses only the original title and body, comments, linked pull
requests, and closing commits. It adds a validated `resolution` object with the
outcome and type, root cause, summary, verified steps, exact evidence URLs, and
confidence. A closed issue is not treated as fixed without supporting evidence,
and model-produced URLs must exactly match a URL supplied in the input evidence.

Invalid model output is retried once. Remaining failures are recorded as
`resolution: null` without stopping later issues, and are retried on a future
run. Existing successful issue numbers are skipped, duplicate output records
are collapsed, and `--limit` is the target total of successfully extracted
resolutions. Use `--overwrite` to start again.

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

The same CLI also accepts enriched issue records:

```bash
python -m app.graph.cli \
  --input data/processed/kubernetes_issues_enriched.jsonl
```

It can also load records containing verified resolutions and their evidence:

```bash
python -m app.graph.cli \
  --input data/processed/kubernetes_issues_with_resolutions.jsonl
```

The loader derives the repository from each issue URL and uses Cypher `MERGE`
for all nodes and relationships. Existing `Repository`, `Issue`, and `Label`
entities are preserved. Enriched records update the issue's `summary`,
`symptom`, and `expected_behavior`, then create `Component`,
`KubernetesVersion`, and `ErrorMessage` nodes connected through
`AFFECTS_COMPONENT`, `REPORTED_IN_VERSION`, and `HAS_ERROR`. Component names
are trimmed and lowercased for matching. Re-running either raw or enriched
loads does not duplicate graph entities, and the loader never clears existing
Neo4j data.

When `resolution_found` is true, the loader merges a stable `Resolution` node
and connects it through `HAS_RESOLUTION`. Pull requests and closing commits are
merged and connected through `LINKED_TO_PR` and `CLOSED_BY`. Only comments cited
by `evidence_urls` are stored. Each cited URL supports the resolution through a
`SUPPORTED_BY` relationship to its matching `Comment`, `PullRequest`, or
`Commit`; unmatched cited URLs become `EvidenceSource` nodes. Records without a
verified resolution create none of these resolution-evidence entities. All
nodes and relationships use `MERGE`, and existing graph data is never deleted.

Stop the service without deleting its data:

```bash
docker compose down
```

## Semantic issue search

After loading issues into Neo4j, generate local embeddings and create the
Neo4j vector index:

```bash
python -m app.retrieval.cli index
```

The command uses `sentence-transformers/all-MiniLM-L6-v2`, which produces
384-dimensional normalized embeddings. Searchable text is built from each
Issue's title, summary, symptom, and expected behavior. A content hash is kept
on the node, so later indexing runs skip issues whose searchable fields have
not changed.

Search the indexed issues:

```bash
python -m app.retrieval.cli search \
  --query "garbage collector keeps processing objects with valid owners" \
  --limit 5
```

Each JSON result contains the issue number, title, summary, URL, and cosine
similarity score. The embedding model runs locally; its first use may download
the model files.

For a GraphRAG-style context object, expand the five closest vector matches
through their components, Kubernetes versions, labels, and error messages:

```bash
python -m app.retrieval.cli graph-search \
  --query "Kubernetes keeps processing objects even though valid owners exist" \
  --limit 5
```

The result is one JSON object containing the query, deduplicated vector-matched
issues with similarity scores and graph metadata, and up to three additional
issues sharing a component or Kubernetes version. This retrieval step is
bounded and does not call FH Genie or another language model. When available,
matched and related issues include `verified_resolution` with its type, root
cause, summary, verified steps, confidence, and supporting evidence URLs.

## Project structure

```text
.
├── src/app/          # FastAPI application package
│   ├── graph/        # Neo4j graph loader and CLI
│   ├── enrichment/   # FH Genie client, models, and JSONL CLI
│   ├── ingestion/    # GitHub issue and resolution-evidence ingestion
│   └── retrieval/    # Local embeddings and Neo4j vector search CLI
├── tests/            # Automated tests
├── .env.example      # Local configuration template
├── docker-compose.yml # Local Neo4j Community Edition service
└── pyproject.toml    # Project metadata and dependencies
```
