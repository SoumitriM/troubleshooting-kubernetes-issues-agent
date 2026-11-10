"""Run the golden dataset against a live API and score every response.

Usage:
    python evals/run_golden.py [--base-url http://127.0.0.1:8000] [--only doc-01,graph-02]

Requires the API, Neo4j, Claude, and the documentation index to be available.
Results are written to evals/results/<timestamp>.json (ignored by Git).
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parent
DATASET = ROOT / "golden_dataset.json"
RESULTS_DIR = ROOT / "results"
REQUEST_TIMEOUT_SECONDS = 180.0


def issue_count() -> int | None:
    """Count Issue nodes so destructive-request cases can prove nothing changed."""

    try:
        from neo4j import GraphDatabase

        from app.graph.loader import GraphSettings

        settings = GraphSettings()
        with GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(
                settings.neo4j_username,
                settings.neo4j_password.get_secret_value(),
            ),
        ) as driver:
            records, _, _ = driver.execute_query("MATCH (i:Issue) RETURN count(i) AS n")
            return records[0]["n"]
    except Exception:
        return None


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def score(response: dict[str, Any], expect: dict[str, Any]) -> list[str]:
    """Return human-readable failures; an empty list means the case passed."""

    failures: list[str] = []
    answer = response.get("answer") or ""
    citations = response.get("citations") or []
    for field in ("status", "intent", "route"):
        if field in expect and response.get(field) not in as_list(expect[field]):
            failures.append(f"{field}={response.get(field)!r}, expected {expect[field]!r}")
    if response.get("route") in expect.get("route_not", []):
        failures.append(f"route={response.get('route')!r} is forbidden")
    for text in expect.get("answer_contains", []):
        if text.casefold() not in answer.casefold():
            failures.append(f"answer missing {text!r}")
    for text in expect.get("answer_not_contains", []):
        if text.casefold() in answer.casefold():
            failures.append(f"answer must not contain {text!r}")
    for text in expect.get("citations_contain", []):
        if not any(text in citation for citation in citations):
            failures.append(f"no citation contains {text!r}")
    prefix = expect.get("citations_prefix")
    if prefix and any(not citation.startswith(prefix) for citation in citations):
        failures.append(f"citation outside {prefix}")
    if len(citations) < expect.get("min_citations", 0):
        failures.append(f"{len(citations)} citations, expected >= {expect['min_citations']}")
    if "max_citations" in expect and len(citations) > expect["max_citations"]:
        failures.append(f"{len(citations)} citations, expected <= {expect['max_citations']}")
    return failures


def run_case(client: httpx.Client, case: dict[str, Any]) -> dict[str, Any]:
    expect = case["expect"]
    before = issue_count() if expect.get("graph_unchanged") else None
    started = time.perf_counter()
    try:
        response = client.post("/api/knowledge/query", json={"query": case["query"]})
        response.raise_for_status()
        body = response.json()
        if case.get("resume"):
            if body.get("status") != "needs_clarification":
                raise RuntimeError(
                    f"expected a clarification before resuming, got {body.get('status')!r}"
                )
            response = client.post(
                f"/api/knowledge/{body['thread_id']}/resume",
                json={"answer": case["resume"]},
            )
            response.raise_for_status()
            body = response.json()
        failures = score(body, expect)
    except Exception as error:
        body = {}
        failures = [f"request failed: {type(error).__name__}: {error}"]
    latency = time.perf_counter() - started
    if expect.get("graph_unchanged"):
        after = issue_count()
        if before is None or after != before:
            failures.append(f"graph Issue count changed or unreadable ({before} -> {after})")
    return {
        "id": case["id"],
        "category": case["category"],
        "query": case["query"],
        "passed": not failures,
        "failures": failures,
        "latency_seconds": round(latency, 2),
        "response": body,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--only", help="Comma-separated case IDs to run")
    args = parser.parse_args()

    cases = json.loads(DATASET.read_text(encoding="utf-8"))["cases"]
    if args.only:
        wanted = set(args.only.split(","))
        cases = [case for case in cases if case["id"] in wanted]

    with httpx.Client(base_url=args.base_url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        ready = client.get("/ready")
        if ready.status_code != 200:
            print(f"API is not ready at {args.base_url} (HTTP {ready.status_code})")
            return 2
        results = []
        for case in cases:
            result = run_case(client, case)
            results.append(result)
            response = result["response"]
            mark = "PASS" if result["passed"] else "FAIL"
            print(
                f"{mark} {result['id']:<18} {result['latency_seconds']:>6.2f}s "
                f"intent={response.get('intent')} route={response.get('route')}"
            )
            for failure in result["failures"]:
                print(f"     - {failure}")

    passed = sum(result["passed"] for result in results)
    by_category: dict[str, list[bool]] = {}
    for result in results:
        by_category.setdefault(result["category"], []).append(result["passed"])
    print(f"\n{passed}/{len(results)} passed")
    for category, outcomes in by_category.items():
        print(f"  {category:<22} {sum(outcomes)}/{len(outcomes)}")

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = RESULTS_DIR / f"{stamp}.json"
    output.write_text(
        json.dumps(
            {"base_url": args.base_url, "passed": passed, "total": len(results), "results": results},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"Results written to {output.relative_to(ROOT.parent)}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
