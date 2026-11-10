# Evaluation

There are two levels of testing:

- **Unit tests** (`python -m pytest`) replace Claude, Neo4j and the embedder
  with fakes. They are fast, free and run anywhere.
- **The golden evaluation** sends real questions to the running API and checks
  the answers end to end, including Claude's routing.

## Golden dataset

[`evals/golden_dataset.json`](../evals/golden_dataset.json) holds 34 cases:

| Category | Cases | What it checks |
|---|---|---|
| `documentation` | 8 | Conceptual questions answered from kubernetes.io with matching citations |
| `graph_analytics` | 10 | Counts, rankings, lists and relationships return the right rows; includes a condition templates cannot express and an empty-template fallback |
| `incident_fix` | 2 | Reports with a verified fix get the fix and its evidence URLs |
| `incident_findings` | 4 | Reports without a verified fix list matching issues, never a fix |
| `clarification` | 4 | Vague reports get a clarifying question and no citations |
| `clarification_resume` | 2 | A vague report plus a follow-up answer reaches the right issue |
| `safety` | 2 | A prompt-injected deletion and an off-topic question are not acted on, and the graph is unchanged |
| `multi_source` | 2 | Questions spanning analytics, incidents and docs go through the research loop and cite each source |

Graph and incident expectations assume the 100 issues in `data/processed/`
are loaded.

## Running it

Start Neo4j, the API and Claude access first (see
[Local development](local-development.md)), then:

```bash
python evals/run_golden.py                                  # all cases
python evals/run_golden.py --only graph-04,incident-find-01 # selected cases
python evals/run_golden.py --base-url http://127.0.0.1:8000
```

Each run prints PASS or FAIL per case with its latency, intent and route, then
totals by category. Full responses go to `evals/results/<timestamp>.json`,
which Git ignores. The command exits with status 1 if any case fails, so it can
gate CI.

A full run makes about 30–60 Claude calls and takes 3–8 minutes. With the
`claude_code` backend these count against the signed-in user's plan.

## Judged quality metrics

`run_golden.py` checks fixed rules. `judge_golden.py` scores the same run for
answer quality:

```bash
python evals/judge_golden.py                       # judge the latest run
python evals/judge_golden.py --results evals/results/<run>.json --only doc-01
JUDGE_MODEL=claude-sonnet-5-5 python evals/judge_golden.py   # a different judge model
```

It gathers evidence independently of the app: the cited and top-retrieved
documentation chunks, the issue records from Neo4j, and the rows of a reference
Cypher query written for each analytics case (`GRAPH_REFERENCES`). A Claude
judge (effort `JUDGE_EFFORT`, default `medium`) then splits each answer into
claims and labels each one supported, unsupported or contradicted. It also
rates each citation, grades correctness (correct, partially correct or
incorrect), and scores completeness and relevance from 1 to 5.

| Metric | Definition |
|---|---|
| Strict / lenient accuracy | Correct answers / all; partially correct counts half in lenient |
| Faithfulness | Supported claims / all claims |
| Claim hallucination rate | Unsupported or contradicted claims / all claims |
| Answers with any hallucination | Answers containing at least one unsupported claim |
| Citation precision | Citations the judge found relevant / all citations |
| Citation validity | Cited URLs that exist in the docs index or graph (deterministic) |
| Expected-source recall | Golden `citations_contain` sources that were cited (deterministic) |
| Incident retrieval precision / recall | Issues shown vs the golden expected issues |
| Graph result precision / recall | Issue numbers listed vs the reference query |
| Intent and clarification P/R/F1 | Per-label precision, recall and F1, plus accuracy and macro F1 |

Results go to `<run>-judged.json` and a readable `<run>-report.md` listing
every unsupported claim, irrelevant citation and missing point.

"Unsupported" means not established by the evidence, not necessarily false.
The judge is a Claude model grading Claude output, so expect some
self-preference. Use `JUDGE_MODEL` to cross-check, and treat small differences
between runs as noise.

## Case format

```json
{
  "id": "clarify-resume-01",
  "category": "clarification_resume",
  "query": "Kubernetes is not working",
  "resume": "The garbage collector in kube-controller-manager repeatedly processes objects with valid owners on Kubernetes v1.36.2",
  "expect": {
    "status": "completed",
    "intent": "INCIDENT_TROUBLESHOOTING",
    "route": "generate_findings",
    "answer_contains": ["#141382"],
    "citations_contain": ["issues/141382"]
  }
}
```

`resume` is optional. When present, the first response must ask for
clarification, and the runner answers it before checking expectations.

| `expect` key | Passes when |
|---|---|
| `status`, `intent`, `route` | The response field equals the value, or one of a list of values |
| `route_not` | The route is none of the listed routes |
| `answer_contains` / `answer_not_contains` | Each string is present / absent (case-insensitive) |
| `citations_contain` | Each string appears in at least one citation |
| `citations_prefix` | Every citation starts with this prefix |
| `min_citations` / `max_citations` | The citation count is within bounds |
| `min_suggestions` | The response offers at least this many follow-up suggestions |
| `graph_unchanged` | The Neo4j `Issue` count is the same before and after the request |

## Interpreting failures

- **Route or intent mismatch.** These usually come from query understanding.
  The API log shows the routed intent and confidence. Rerun with
  `python -m app.agent.cli --query "…" --debug` to see the full structured
  analysis. Decide whether the prompt or the expectation is wrong
  before changing either.
- **Missing answer text or citation.** Check the retrieval thresholds and that
  the expected issue is loaded and embedded.
- **Request failed.** The API or a dependency is down. Check `/ready` and the
  API log.

Responses vary between runs. Compare more than one run before treating a
single failure as a regression, and add paraphrased variants when a case seems
to pass by chance.
