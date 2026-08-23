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
Every resolution also records whether its disposition is verified and whether
an actionable fix is available. Only `fixed` and `configuration` outcomes with
supported verified steps can be actionable; closure without a fix, duplicates,
non-reproducible outcomes, and unknown outcomes are never actionable solutions.

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

GraphRAG fetches a larger vector candidate pool internally and accepts at most
five direct matches after deterministic reranking by vector similarity, exact
components and Kubernetes versions, and distinctive error or symptom terms.
Comparison is lowercase and whitespace-trimmed; hyphens, underscores, and
spaces are equivalent, while original values are retained for display. A
distinctive status such as `CrashLoopBackOff` must occur in both the query and
candidate, explicit version conflicts are rejected, and component overlap alone
is insufficient without a distinctive symptom match. Every accepted match
includes a `relevance` explanation. Results outside the configured score window
relative to the best qualifying match are removed rather than used to fill five
positions. To inspect rejected candidates, scores, and rejection reasons, add
`--debug` to `graph-search`:

```bash
python -m app.retrieval.cli graph-search \
  --query "garbage collector keeps processing objects with valid owners" \
  --limit 5 \
  --debug
```

The standalone retrieval CLI operates in `raw_query_only` mode because it has
no incident-analysis stage. Its JSON explicitly reports that mode and exposes
vector scores only—not component, version, error, or symptom matching fields.
Both Hugging Face and Transformers progress bars are disabled before model
initialization; stdout remains JSON and genuine errors remain on stderr.

Configure reranking with `RETRIEVAL_MIN_VECTOR_SCORE`,
`RETRIEVAL_EXCEPTIONAL_VECTOR_SCORE`, and
`RETRIEVAL_MIN_FINAL_RELEVANCE_SCORE`, and
`RETRIEVAL_MIN_RELATIVE_SCORE_RATIO`. Graph expansion starts only from accepted
direct matches, returns at most three related issues, and requires either two
shared components or a shared component plus the same version. With no accepted
direct match, the context sets `no_close_match` and skips graph expansion.

For a GraphRAG-style context object, expand the five closest vector matches
through their components, Kubernetes versions, labels, and error messages:

```bash
python -m app.retrieval.cli graph-search \
  --query "Kubernetes keeps processing objects even though valid owners exist" \
  --limit 5
```

The result is one JSON object containing the query, deduplicated vector-matched
issues with similarity scores and graph metadata, and up to three additional
issues satisfying the stricter shared-component/version expansion rules. This
retrieval step is bounded and does not call FH Genie or another language model.
When available,
matched and related issues include `verified_disposition` with its type, root
cause, summary, confidence, and supporting evidence URLs. A separate
`actionable_fix` object is returned only for supported `fixed` or
`configuration` outcomes with verified steps. In particular,
`closed_without_fix` is retained as disposition context but never offered as a
solution.

## Evidence-grounded incident assistant

Run the LangGraph incident assistant with a natural-language report:

```bash
python -m app.agent.cli \
  --query "Non-admin users can enable DRA adminAccess by labeling their namespace"
```

Normal mode returns only `route`, `answer`, and `citations`. Add `--debug` to
print readable JSON containing the sanitized LangGraph update stream:

```bash
python -m app.agent.cli \
  --query "Pods enter CrashLoopBackOff after the kubelet restarts on Kubernetes v1.35" \
  --debug
```

Debug output shows the structured incident analysis, vector candidate issue
numbers and scores, deterministic reranking signals and rejection reasons,
accepted and graph-expanded issue numbers, the evidence decision, executed
nodes, final route, and route reason. It intentionally excludes API keys,
environment variables, prompts, complete issue bodies, and full candidate
records. Both modes reserve stdout for one valid JSON document; model progress
and actionable diagnostics go to stderr, while the optional unauthenticated
Hugging Face download advisory is suppressed.

The workflow first uses FH Genie to extract the symptom, components, Kubernetes
version, and error messages. A deterministic clarification gate prevents vague
reports such as “Kubernetes is not working” from reaching Neo4j at all. Detailed
reports proceed to GraphRAG retrieval, where results must satisfy both the
configured similarity threshold and a semantic, component, or version overlap.
Those extracted fields are validated once as a shared `QueryAnalysis` object and
passed intact to GraphRAG with the original query text. The reranker uses only
`QueryAnalysis.components` for components, `kubernetes_version` for version
comparison, `error_messages` for errors, and `symptom` for symptom terms; it does
not rediscover structured fields from raw query tokens.
Error-state matches and symptom-term matches are reported separately. A
lightweight, rule-based Snowball stemmer compares ordinary symptom words with a
generic denylist, so restart, fail, work, and break inflections do not count as
distinctive overlap. A small built-in English stopword list is also stemmed and
applied after normalization, so auxiliary words such as `have`, `after`, `when`,
and `their` cannot increase relevance. It requires no downloaded NLP model.
Meaningful Kubernetes vocabulary remains available, while technical identifiers,
Kubernetes error states, versions, paths, and code symbols bypass stemming and
retain their original spelling in diagnostics. Version diagnostics distinguish
`exact`, `compatible_build`, `compatible_minor`,
`conflict`, and `unknown`; for example, `v1.35.x` is compatible with `v1.35`
but is not labeled exact, and `v1.36.2-eks-*` is labeled as a compatible vendor
build for `v1.36.2`.
Strong symptom/error/component matches from an explicitly conflicting version
are returned separately as `cross_version_matches`. They are not direct matches,
do not seed graph expansion, and their resolutions are never treated as applying
to the requested Kubernetes version.
Routing is deterministic:

- `generate_fix` is used only when similarity is sufficient and GraphRAG
  provides a verified actionable `fixed` or `configuration` resolution with
  exact steps and evidence URLs.
- `generate_findings` summarizes relevant incidents and components while
  explicitly stating that no verified actionable fix was found. Direct vector
  matches and graph-expanded related issues are shown in separate sections.
- `request_clarification` asks for missing version, component, logs, or error
  details when retrieval is insufficient. If reranking found no accepted direct
  match, it explicitly says no close historical incident was found, acknowledges
  already-known error/component/version facts, and asks only for missing runtime
  evidence such as logs, pod events, exit reason, scope, and recent changes.

Findings cite only the issue pages whose details appear in the answer.
Resolution evidence URLs are cited only when `generate_fix` actually uses that
verified resolution.

FH Genie selects an actionable resolution, but its answer summary, steps, and
citations must exactly match retrieved evidence. Unsupported summaries,
commands, steps, and URLs are rejected. `closed_without_fix`, duplicate,
non-reproducible, and unknown
dispositions are never treated as fixes. Configure deterministic routing with
`AGENT_RELEVANCE_THRESHOLD` and `AGENT_ACTIONABLE_FIX_THRESHOLD`; both values
must be between 0 and 1.

## Project structure

```text
.
├── src/app/          # FastAPI application package
│   ├── agent/        # LangGraph evidence-grounded incident assistant
│   ├── graph/        # Neo4j graph loader and CLI
│   ├── enrichment/   # FH Genie client, models, and JSONL CLI
│   ├── ingestion/    # GitHub issue and resolution-evidence ingestion
│   └── retrieval/    # Local embeddings and Neo4j vector search CLI
├── tests/            # Automated tests
├── .env.example      # Local configuration template
├── docker-compose.yml # Local Neo4j Community Edition service
└── pyproject.toml    # Project metadata and dependencies
```
