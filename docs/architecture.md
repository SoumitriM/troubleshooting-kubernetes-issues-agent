# Architecture

The platform answers Kubernetes questions from three sources of evidence: a
Neo4j graph of real GitHub issues and their verified resolutions, aggregate
queries over that graph, and an index of the official Kubernetes
documentation. Claude interprets each question and writes answers, but every
fact, step and link it returns must come from retrieved evidence.

## Components

```text
React UI (Vite, :5173) ──/api──▶ FastAPI (:8000)
                                   │
                                   ▼
                         LangGraph workflow ──▶ Claude (API or Claude Code)
                         │        │        │
                         ▼        ▼        ▼
                 Neo4j graph   Neo4j     SQLite docs index
                 + vector      read-only (data/kubernetes_docs.sqlite)
                 index         analytics
                         │
                         ▼
                 SQLite checkpoints (data/checkpoints.sqlite)
                 SQLite chat history (data/conversations.sqlite)
```

| Component | Code | Role |
|---|---|---|
| HTTP API | [`src/app/main.py`](../src/app/main.py), [`src/app/api/`](../src/app/api/) | Builds shared clients at startup and exposes the chat, knowledge and incident endpoints |
| Conversation store | [`src/app/chat/store.py`](../src/app/chat/store.py) | Chat conversations and messages in `data/conversations.sqlite` |
| Workflow | [`src/app/agent/workflow.py`](../src/app/agent/workflow.py), [`nodes.py`](../src/app/agent/nodes.py) | LangGraph state machine that routes each question |
| LLM client | [`src/app/enrichment/claude_client.py`](../src/app/enrichment/claude_client.py), [`prompts.py`](../src/app/enrichment/prompts.py) | Every Claude call, with schema-constrained output and evidence checks |
| Incident retrieval | [`src/app/retrieval/graph_retriever.py`](../src/app/retrieval/graph_retriever.py) | Vector search over issues, deterministic reranking and graph expansion |
| Graph analytics | [`src/app/graph/analytics.py`](../src/app/graph/analytics.py), [`cypher_validation.py`](../src/app/graph/cypher_validation.py) | Validated, read-only Cypher execution |
| Documentation search | [`src/app/documentation/store.py`](../src/app/documentation/store.py) | Hybrid search (vector, BM25 and title match combined with RRF) over documentation chunks held in memory |
| Embeddings | [`src/app/retrieval/embeddings.py`](../src/app/retrieval/embeddings.py) | Local `all-MiniLM-L6-v2` model (384 dimensions) |
| UI | [`frontend/src/`](../frontend/src/) | Chat interface with a sidebar of saved conversations |

## Request workflow

Every question enters `understand_query`, which makes one Claude call. That
call returns a normalized query, an intent with a confidence score, the
components, versions, symptoms and error messages it found, whether the text
describes a concrete incident, and, for analytics questions, a structured
operation. It also lists the evidence sources the question needs
(`incidents`, `analytics`, `docs`). A question that needs two or more goes to
the research route; every other question follows one of four single-source
routes.

```text
understand_query
 ├─ 2+ sources needed ─▶ research (Claude picks ≤4 read-only tool calls) ─▶ answer
 │                          └─ on failure ─▶ the question's primary route below
 ├─ GENERAL_KUBERNETES_QUESTION ─▶ retrieve_kubernetes_docs ─▶ answer_from_kubernetes_docs
 ├─ GRAPH_ANALYTICS ─▶ generate_cypher ─▶ validate_cypher ─▶ execute_neo4j ─▶ answer_from_graph
 │                          ▲    ▲              │ invalid ×2       │ no rows (once)
 │                          │    └ regenerate ──┘ ─▶ graph_query_failed
 │                          └───────────── prepare_alternative ◀──┘
 ├─ INCIDENT_TROUBLESHOOTING ─▶ analyze_incident ─▶ retrieve_evidence ─▶ assess_evidence
 │                                    │                                     ├─▶ generate_fix
 │                                    │ not concrete                        ├─▶ generate_findings
 │                                    ▼                                     ▼
 └─ NEEDS_CLARIFICATION ─────▶ request_clarification ◀───────── insufficient evidence
                                      │ user answers (resume)
                                      └──────────▶ understand_query
```

### Research: questions that span several sources

`research` ([`nodes.py`](../src/app/agent/nodes.py)) is the one agentic step.
Claude chooses each step and when to stop, within these bounds:

- Each step is one structured action: `search_incidents(query)`,
  `graph_analytics(operation, group_by, components, versions)`,
  `search_docs(query)` or `final_answer`.
- The tools are read-only and reuse the single-source paths: GraphRAG
  retrieval with the verified-fix rules, the Cypher templates and validator,
  and the hybrid documentation search. The agent never writes Cypher.
- At most four tool calls, then it must answer.
- The final answer may cite only URLs that the tools returned (checked by
  `ClaudeClient.research_step`), must use only facts from the observations,
  and answers each part of the question separately.
- If the loop fails, the question continues on its primary route.

Example: "How many issues affect etcd, and what does etcd do according to the
documentation?" calls `graph_analytics` (COUNT, component etcd) and
`search_docs`, then answers both parts with the etcd docs pages cited.

### Documentation answers

`retrieve_kubernetes_docs` returns six documentation chunks from a hybrid
search ([`store.py`](../src/app/documentation/store.py)). Three rankings are
fused with reciprocal rank fusion: embedding similarity, BM25 keyword
scoring, and a match between the question and page titles. The keyword
tokenizer splits CamelCase API names, so `PersistentVolumeClaim` also matches
"persistent volume claim". Generated API-reference and feature-gate pages are
ranked lower than concept and task pages, and at most three chunks come from
one page. Claude answers from those chunks only, must not add facts from
general knowledge, and may cite only their source URLs. If
the call fails or cites anything else, the answer falls back to an excerpt of
the best chunk with that chunk's URL. With no chunks, the reply says nothing
relevant was found.

### Graph analytics

Common question shapes (counts, top-N, grouping by component or version,
component relationships, issue lists) map to fixed Cypher templates in
`IncidentNodes._analytics_cypher`; Claude only supplies the structured
operation and filters. Other analytics questions ask Claude to write Cypher.
Every query, template or generated, passes `CypherValidator` before it runs:

- No write, procedure or admin keywords, comments or multiple statements.
- Only node labels and relationship types from [`schema.py`](../src/app/graph/schema.py).
- `MATCH` and `RETURN` are required, every literal must be a `$parameter`, and
  `LIMIT` is capped at 100.

Templates match component names with or without a `kube-` prefix, so
"scheduler" and "kube-scheduler" give the same result. They also fetch one
row beyond the display limit, so the answer can say when a list was cut
short or a result ties with the last one shown.

Templates handle only component and version filters. When the question
adds another condition ("has a verified fix", "linked to a pull request",
"closed in July"), `understand_query` lists it in `extra_conditions` and
Claude writes the query instead, so no condition is silently dropped.

If the first query, template or Claude-written, returns no rows,
`prepare_alternative` runs once. It looks up component names in the graph
that resemble the question's terms (for example "draadminaccess" for "DRA
admin access") and asks Claude for one broader query using them. That query
is validated like any other, and the answer starts by saying a broader
search was used. If it also finds nothing, or fails validation, the answer
says nothing matched.

Validated queries run in a Neo4j read transaction.
[`formatting.py`](../src/app/graph/formatting.py) renders the rows as plain
text without calling Claude, so the answer cannot add facts. A single value
becomes a labelled figure, counts become lines such as `kubelet: 13 issues`,
and issue rows become linked lines whose URLs are returned as citations.
Duplicate rows are removed. A query that fails
validation twice ends with a request to rephrase.

### Incident troubleshooting

1. `analyze_incident` sends a non-concrete report (for example, "Kubernetes is
   not working") straight to clarification without querying Neo4j.
2. `retrieve_evidence` runs vector search over Issue embeddings, then reranks
   candidates deterministically. It checks components, versions, error states
   and distinctive symptom terms, rejects conflicting versions, and keeps up to
   five direct matches. A component name counts as a component match only,
   never also as a symptom term. Graph expansion adds up to three related
   issues that share two components, or a component and a version.
   Claude then labels each candidate `same_failure`, `related` or `unrelated`
   (`verify_incident_matches`). Unrelated candidates are removed, and related
   ones are demoted to the related list when a same-failure match exists.
   If every candidate is unrelated, the request goes to clarification with
   "no close match". Verification can only narrow the rule-based result; if
   the Claude call fails, the reranked matches are used unchanged.
3. `assess_evidence` chooses which GitHub evidence is usable:
   - `generate_fix` when the top score is at least
     `AGENT_ACTIONABLE_FIX_THRESHOLD` and a match carries a verified `fixed` or
     `configuration` resolution with steps and evidence URLs. Claude's summary,
     steps and citations must exactly match that evidence.
   - `generate_findings` when matches are relevant
     (`AGENT_RELEVANCE_THRESHOLD`) but no verified fix exists. Direct and
     graph-expanded matches are listed separately with their issue URLs.
   - `request_clarification` otherwise.

4. `troubleshoot_from_docs` makes the official documentation the primary
   troubleshooting source. It retrieves six documentation chunks and asks
   Claude for the likely causes and how to check each, grounded in those
   chunks, ending with one question that narrows the cause down. The GitHub
   evidence from step 3 is secondary and incident-specific: verified fix steps
   or a list of similar reports are appended verbatim under the docs answer,
   with their links as citations, and the route stays `generate_fix` or
   `generate_findings`. With no usable GitHub evidence the route is
   `docs_troubleshooting`. If the docs cannot explain the problem, the incident
   falls back to the GitHub-only route from step 3.

### Clarification

When the API runs with its SQLite checkpointer, `request_clarification`
pauses the thread with a LangGraph interrupt. Before it, `prepare_clarification`
asks Claude for a conversational question: it acknowledges what is already
known, asks for the one or two missing details that would help most, may
point to a relevant documentation page or a read-only diagnostic command, and
offers likely answers as suggestions. If that call fails, the question falls
back to a list of the missing details. The client resumes with the
user's answer, and the workflow re-runs `understand_query` on the original
question plus every answer. After three clarification rounds the thread ends
and lists what is still missing.

### Conversation memory

The chat API ([`src/app/api/chat.py`](../src/app/api/chat.py)) stores every
message in `data/conversations.sqlite`
([`src/app/chat/store.py`](../src/app/chat/store.py)). Each new message starts
a new workflow thread, and the six most recent earlier messages go into the
`conversation_history` state field. `understand_query` passes them to Claude,
which rewrites a follow-up such as "and the scheduler?" into a standalone
question before routing. Retrieval and answers then use only that standalone
question, so the history cannot add evidence. A question on a new topic does
not inherit details from earlier turns.

If the previous assistant message asked for clarification, the next message
answers that question: it resumes the paused thread instead of starting a new
one.

Questions that are not about Kubernetes, and requests to change data or run
commands, are marked `in_scope: false` by `understand_query`. They get a short
scope answer instead of a clarification question.

If the Claude call in `understand_query` fails, the request is not sent to
clarification. It ends with "The query-understanding service is temporarily
unavailable", because more detail from the user would not help.

### Conversational replies

Every reply ends with up to three follow-up `suggestions`, shown in the UI as
buttons under the latest message. Incident, analytics, clarification and
scope replies get their wording from one `compose_reply` call that may only
rephrase the evidence the route already gathered; the evidence itself (fix
steps, issue lists, analytics rows) is appended verbatim. See
[LLM integration](llm-integration.md#conversational-replies).

## Safety model

- **Grounded output.** Fix summaries, steps, citations and evidence URLs are
  checked against the supplied evidence, and any mismatch rejects the reply.
  See [LLM integration](llm-integration.md#evidence-checks).
- **Read-only analytics.** Generated Cypher is checked against an allow-list
  and executed in a read transaction.
- **Untrusted input.** Prompts tell Claude to treat user text and issue content
  as data, never as instructions. The golden dataset includes a prompt-injected
  deletion request and verifies the graph is unchanged afterwards.
- **No leaks in responses.** API errors return a generic 503. Prompts, issue
  bodies, keys and stack traces stay in server logs.
- **Unresolved is not fixed.** Issues closed without a fix, duplicates and
  non-reproducible reports are shown as context, never as solutions.

## Data pipeline

The graph and indexes are built offline in this order. Each command is
resumable and is described in the [README](../README.md).

| Step | Command | Output |
|---|---|---|
| 1. Ingest issues | `python -m app.ingestion.cli` | `data/raw/*.jsonl` |
| 2. Enrich with Claude | `python -m app.enrichment.cli` | summary, symptom, components, errors, versions |
| 3. Fetch resolution evidence | `python -m app.ingestion.resolution_cli` | comments, linked PRs, closing commits |
| 4. Extract resolutions with Claude | `python -m app.enrichment.resolution_cli` | verified resolution with evidence URLs |
| 5. Load the graph | `python -m app.graph.cli --input …` | Neo4j nodes and relationships (`MERGE` only) |
| 6. Embed issues | `python -m app.retrieval.cli index` | `issue_embedding_index` vector index |
| 7. Index documentation | `python -m app.documentation.cli fetch` then `index` | `data/kubernetes_docs.sqlite` |
