# AI Incident Resolution Platform

Initial FastAPI service for an AI-assisted incident-resolution platform. It
provides a health endpoint, GitHub issue ingestion, FH Genie issue enrichment,
a local Neo4j graph loader, semantic issue search, and an evidence-grounded
incident-analysis API. It does not yet include a frontend or Kubernetes
deployment controller; local Kubernetes manifests are provided under `k8s/`.

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

## Docker Compose

Copy `.env.example` to `.env` and configure the Neo4j credentials and FH Genie
settings before building. Compose injects `FH_GENIE_KEY`,
`FH_GENIE_BASE_URL`, and `FH_GENIE_MODEL` from that runtime file; `.env` is
excluded from the image build context and must not be committed.

Build the FastAPI image:

```bash
docker compose build app
```

Start Neo4j and the application in the background:

```bash
mkdir -p data
docker compose up -d
```

The application waits for Neo4j's health check and connects to it internally at
`neo4j://neo4j:7687`. The existing Neo4j named volume remains
`neo4j_data`; application data is bind-mounted from `./data` to `/app/data`, so
LangGraph checkpoints in `data/checkpoints.sqlite` survive container restarts.
The application runs as a non-root user with one Uvicorn worker and one Compose
replica. Do not scale this service horizontally while it uses the local SQLite
checkpointer.

Check service status and follow application logs:

```bash
docker compose ps
docker compose logs -f app
```

Check the application health endpoint:

```bash
curl http://127.0.0.1:8000/health
```

Start a checkpointed incident conversation:

```bash
curl -X POST http://127.0.0.1:8000/api/incidents/analyze \
  -H 'Content-Type: application/json' \
  -d '{"query":"Kubernetes is not working","thread_id":"demo-thread-1"}'
```

Resume it after the API requests clarification:

```bash
curl -X POST http://127.0.0.1:8000/api/incidents/demo-thread-1/resume \
  -H 'Content-Type: application/json' \
  -d '{"answer":"The garbage collector repeatedly processes objects with valid owners on Kubernetes v1.36.2"}'
```

Stop and remove the containers and network without deleting the existing named
Neo4j volume:

```bash
docker compose down
```

Do not add `--volumes` to that command unless you explicitly intend to delete
the Neo4j data volume.

## Kubernetes on Docker Desktop

The manifests in `k8s/` deploy one FastAPI pod and one Neo4j StatefulSet into
the `incident-ai` namespace. The app image is local-only, uses
`imagePullPolicy: Never`, and must be named `incident-resolution-ai-app:latest`:

```bash
docker build -t incident-resolution-ai-app:latest .
```

Enable Kubernetes in Docker Desktop, then update the non-secret
`FH_GENIE_BASE_URL` and `FH_GENIE_MODEL` placeholders in
`k8s/01-configmap.yaml`. Create the namespace and runtime Secret without writing
real credentials into a manifest:

```bash
kubectl apply -f k8s/00-namespace.yaml

kubectl -n incident-ai create secret generic incident-ai-secrets \
  --from-literal=FH_GENIE_KEY="$FH_GENIE_KEY" \
  --from-literal=NEO4J_USERNAME="$NEO4J_USERNAME" \
  --from-literal=NEO4J_PASSWORD="$NEO4J_PASSWORD" \
  --from-literal=NEO4J_AUTH="$NEO4J_USERNAME/$NEO4J_PASSWORD"
```

Set those shell variables securely before running the command. The committed
`k8s/02-secret.example.yaml` is a schema example containing placeholders only;
do not put real values into the tracked file. It is intentionally excluded from
`k8s/kustomization.yaml`.

Deploy the non-secret resources:

```bash
kubectl apply -k k8s
```

The application connects to `neo4j://neo4j:7687`. Neo4j data and
`/app/data/checkpoints.sqlite` use separate `ReadWriteOnce` claims. The FastAPI
Deployment is fixed at one replica with a `Recreate` strategy because the local
SQLite checkpointer cannot safely be shared across replicas. Neo4j service-link
environment injection is disabled because Kubernetes-generated `NEO4J_PORT_*`
variables are otherwise interpreted as invalid Neo4j configuration settings.

Wait for both workloads and verify their services and claims:

```bash
kubectl -n incident-ai rollout status statefulset/neo4j --timeout=5m
kubectl -n incident-ai rollout status deployment/incident-resolution-ai --timeout=10m
kubectl -n incident-ai get pods,services,persistentvolumeclaims
```

Forward the API locally and verify its health and incident endpoint:

```bash
kubectl -n incident-ai port-forward service/incident-resolution-ai 8000:8000
```

In another terminal:

```bash
curl http://127.0.0.1:8000/health

curl -X POST http://127.0.0.1:8000/api/incidents/analyze \
  -H 'Content-Type: application/json' \
  -d '{"query":"Kubernetes is not working","thread_id":"k8s-demo-1"}'
```

For local Neo4j Browser access, forward its HTTP service port:

```bash
kubectl -n incident-ai port-forward service/neo4j 7474:7474
```

Stop the workloads and remove networking/configuration while preserving both
persistent claims:

```bash
kubectl -n incident-ai delete deployment incident-resolution-ai
kubectl -n incident-ai delete statefulset neo4j
kubectl -n incident-ai delete service incident-resolution-ai neo4j
kubectl -n incident-ai delete configmap incident-ai-config
kubectl -n incident-ai delete secret incident-ai-secrets
```

Deleting the `incident-ai` namespace or either PVC permanently removes the
associated Kubernetes deployment state and may delete stored data, depending on
the cluster's storage-class reclaim policy.

Start or continue an incident conversation through the LangGraph workflow. A
stable `thread_id` identifies the checkpointed conversation:

```bash
curl -X POST http://127.0.0.1:8000/api/incidents/analyze \
  -H 'Content-Type: application/json' \
  -d '{"query":"Kubernetes is not working","thread_id":"demo-thread-1"}'
```

An underspecified report pauses with a safe public response:

```json
{
  "thread_id": "demo-thread-1",
  "status": "needs_clarification",
  "question": "Please provide: specific observed behaviour, exact error message or error code, Kubernetes version, relevant logs.",
  "known_details": {},
  "missing_details": [
    "specific observed behaviour",
    "exact error message or error code",
    "affected component",
    "Kubernetes version",
    "relevant logs"
  ]
}
```

Resume the same checkpoint with the user's additional evidence:

```bash
curl -X POST http://127.0.0.1:8000/api/incidents/demo-thread-1/resume \
  -H 'Content-Type: application/json' \
  -d '{"answer":"The garbage collector repeatedly processes objects with valid owners on Kubernetes v1.36.2"}'
```

A completed response contains only the public result:

```json
{
  "thread_id": "demo-thread-1",
  "status": "completed",
  "route": "generate_findings",
  "answer": "...",
  "citations": ["https://github.com/kubernetes/kubernetes/issues/141382"]
}
```

Queries and clarification answers accept at most 4,000 characters; thread IDs
accept up to 128 letters, digits, dots, underscores, colons, or hyphens. The
workflow merges facts from the original query and every clarification. It asks
at most three times, then safely reports what is still missing. FastAPI owns one
`AsyncSqliteSaver` at `data/checkpoints.sqlite`, one Neo4j driver, one embedding
model, and one compiled workflow for its lifespan and closes persistent
resources during shutdown. Checkpoint deserialization uses strict MessagePack
allowlisting without pickle fallback. Tests use `InMemorySaver` and make no FH
Genie or Neo4j calls.

The API never exposes checkpoint state, workflow debug data, embeddings, prompts,
configuration, or secrets. If Neo4j, FH Genie, the embedding model, or another
required dependency is unavailable, incident endpoints return a generic HTTP
503 while `/health` remains available.

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
│   ├── api/          # Incident-analysis HTTP routes and Pydantic schemas
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
