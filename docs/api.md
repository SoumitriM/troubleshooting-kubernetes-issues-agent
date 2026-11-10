# HTTP API

The FastAPI app serves on port 8000. Interactive OpenAPI docs are at
`/docs` while it runs.

## Health

| Endpoint | Response |
|---|---|
| `GET /health` | `200 {"status":"healthy"}` whenever the process is up |
| `GET /ready` | `200 {"status":"ready"}` once the workflow is built, otherwise `503` |

## Chat

The UI uses these endpoints. A conversation keeps every message in
`data/conversations.sqlite`, so later questions can refer to earlier ones
("how many are there?", "and the scheduler?").

### `POST /api/chat`

```json
{"message": "What are the most common kubelet issues in v1.35?", "conversation_id": "optional"}
```

- `message`: 1–4000 characters, trimmed, not blank.
- `conversation_id`: omit it to start a new conversation; send the returned id
  to continue one. Up to 64 characters from `A-Z a-z 0-9 . _ -`. An unknown id
  returns `404`.

Response:

```json
{
  "conversation_id": "8811f3ba6add40cb8f21312c0d1a59f1",
  "message": {
    "role": "assistant",
    "content": "Graph results:\n- {\"total_issues\": 2}",
    "status": "completed",
    "intent": "GRAPH_ANALYTICS",
    "route": "answer_from_graph",
    "citations": [],
    "created_at": "2026-11-03T11:31:37.439+00:00"
  }
}
```

`status`, `intent`, `route` and `citations` have the same values as in the
knowledge endpoints below. When `status` is `needs_clarification`, the next
message in the conversation is treated as the answer to that question.

### Conversation history

| Endpoint | Returns |
|---|---|
| `GET /api/chat/conversations` | The 50 most recently updated conversations: `id`, `title` (the first message), `created_at`, `updated_at` |
| `GET /api/chat/conversations/{id}` | `{"id", "messages": [...]}`, with user and assistant messages in order |
| `DELETE /api/chat/conversations/{id}` | `204`, or `404` if it does not exist |

## Knowledge assistant

Single-question endpoints without conversation memory. They route every
question type: documentation, graph analytics, incidents and clarification.

### `POST /api/knowledge/query`

```json
{"query": "What does the kubelet do?", "thread_id": "optional-id"}
```

- `query`: 1–4000 characters, trimmed, not blank.
- `thread_id`: optional; up to 128 characters from `A-Z a-z 0-9 . _ : -`,
  starting with a letter or digit. A new UUID is generated when it is omitted.
  Reusing an existing thread returns `409`.

Response:

```json
{
  "thread_id": "3c978c14-…",
  "status": "completed",
  "intent": "GENERAL_KUBERNETES_QUESTION",
  "route": "general_answer",
  "answer": "The kubelet is the main node agent…",
  "citations": ["https://kubernetes.io/docs/reference/command-line-tools-reference/kubelet/"]
}
```

| Field | Values |
|---|---|
| `status` | `completed` or `needs_clarification` |
| `intent` | `GENERAL_KUBERNETES_QUESTION`, `GRAPH_ANALYTICS`, `INCIDENT_TROUBLESHOOTING`, `NEEDS_CLARIFICATION` |
| `route` | `general_answer`, `answer_from_graph`, `generate_fix`, `generate_findings`, `request_clarification`, `research_answer` (multi-source) |
| `answer` | Plain text with `-` bullet lists; for `needs_clarification`, the question to ask |
| `citations` | Source URLs used by the answer; empty for analytics and clarification |

### `POST /api/knowledge/{thread_id}/resume`

Answers a clarification question on a thread whose last response had
`"status": "needs_clarification"`.

```json
{"answer": "kube-controller-manager on v1.36.2 keeps reprocessing objects with valid owners"}
```

The response has the same shape as `/query`. It can ask another question, up
to three rounds in total. Returns `404` if the thread does not exist or is not
waiting for an answer.

```bash
curl -s -X POST localhost:8000/api/knowledge/query \
  -H 'content-type: application/json' -d '{"query":"Kubernetes is not working"}'
# → {"thread_id":"abc…","status":"needs_clarification",…}
curl -s -X POST localhost:8000/api/knowledge/abc…/resume \
  -H 'content-type: application/json' -d '{"answer":"…details…"}'
```

## Incident endpoints

These run the same workflow but return an incident-specific shape.

### `POST /api/incidents/analyze`

Request is the same as `/api/knowledge/query`. A completed response:

```json
{"thread_id": "…", "status": "completed", "route": "generate_fix", "answer": "…", "citations": ["…"]}
```

A clarification response includes structured detail:

```json
{
  "thread_id": "…",
  "status": "needs_clarification",
  "question": "I already have component kubelet. Please provide: …",
  "known_details": {"components": ["kubelet"]},
  "missing_details": ["exact error message or error code", "relevant logs"]
}
```

### `POST /api/incidents/{thread_id}/resume`

Same contract as the knowledge resume endpoint, returning the incident shape.

## Errors

| Status | When |
|---|---|
| `409` | `thread_id` already exists |
| `404` | Resume on a thread that is unknown or not awaiting clarification |
| `422` | Invalid request body or `thread_id` |
| `503` | Dependencies not initialised, or an unexpected failure. The body is generic; details are only in server logs |

If Claude is unreachable, requests still return `200` with an answer saying
the query-understanding service is temporarily unavailable.
