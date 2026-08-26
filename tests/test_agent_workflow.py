from typing import Any

from app.agent.nodes import AgentSettings, IncidentNodes
from app.agent.state import GroundedFixAnswer, QueryAnalysis
from app.agent.workflow import build_workflow
from app.enrichment.models import IssueEnrichment


class FakeFHGenie:
    def __init__(self) -> None:
        self.fix_calls = 0

    def enrich(self, title: str, body: str | None) -> IssueEnrichment:
        assert title
        assert body is None
        normalized = title.lower()
        if normalized in {
            "kubernetes is not working",
            "my kubernetes cluster is not working",
            "some kubernetes objects behave incorrectly",
            "something is broken",
            "there is an error",
        }:
            return IssueEnrichment(
                summary=None,
                symptom=title,
                expected_behavior=None,
                components=[],
                error_messages=[],
                kubernetes_versions=[],
            )
        if "garbage collector" in normalized:
            return IssueEnrichment(
                summary=None,
                symptom=(
                    "Garbage collector repeatedly processes objects with valid owners"
                ),
                expected_behavior=None,
                components=["garbage collector"],
                error_messages=[],
                kubernetes_versions=["v1.36.2"],
            )
        if "crashloopbackoff" in normalized:
            return IssueEnrichment(
                summary=None,
                symptom="Pods enter CrashLoopBackOff after kubelet restart",
                expected_behavior=None,
                components=["kubelet"],
                error_messages=["CrashLoopBackOff"],
                kubernetes_versions=["v1.35"],
            )
        return IssueEnrichment(
            summary=None,
            symptom="DRA adminAccess can be enabled",
            expected_behavior=None,
            components=["DRA"],
            error_messages=[],
            kubernetes_versions=["v1.32"],
        )

    def generate_grounded_fix(
        self,
        user_query: str,
        actionable_fixes: list[dict[str, Any]],
    ) -> GroundedFixAnswer:
        self.fix_calls += 1
        assert user_query
        assert actionable_fixes
        return GroundedFixAnswer(
            answer="Authorization corrected",
            verified_steps=["Enable the admission policy"],
            citations=["https://github.com/kubernetes/kubernetes/pull/1"],
        )


class FakeRetriever:
    def __init__(self, context: dict[str, Any]) -> None:
        self.context = context
        self.calls = 0
        self.query_analysis: QueryAnalysis | None = None

    def search(
        self,
        *,
        query_text: str,
        query_analysis: QueryAnalysis,
        limit: int,
        debug: bool,
    ) -> dict[str, Any]:
        self.calls += 1
        assert query_text
        assert limit == 5
        assert debug is False
        self.query_analysis = query_analysis
        return self.context


def matched_issue(
    score: float,
    *,
    actionable: bool,
    resolution_type: str = "fixed",
) -> dict[str, Any]:
    evidence_url = "https://github.com/kubernetes/kubernetes/pull/1"
    return {
        "issue_number": 1,
        "title": "DRA authorization issue",
        "summary": "Namespace labels enabled admin access",
        "url": "https://github.com/kubernetes/kubernetes/issues/1",
        "score": score,
        "components": ["DRA"],
        "verified_disposition": {
            "disposition_verified": True,
            "actionable_fix_available": actionable,
            "resolution_type": resolution_type,
            "evidence_urls": [evidence_url],
        },
        "actionable_fix": (
            {
                "resolution_type": resolution_type,
                "resolution_summary": "Authorization corrected",
                "verified_steps": ["Enable the admission policy"],
                "confidence": "high",
                "evidence_urls": [evidence_url],
            }
            if actionable
            else None
        ),
    }


def run_workflow(
    context: dict[str, Any],
    query: str = "Non-admin users can enable DRA adminAccess",
) -> tuple[dict[str, Any], FakeFHGenie, FakeRetriever]:
    fh_genie = FakeFHGenie()
    retriever = FakeRetriever(context)
    nodes = IncidentNodes(
        fh_genie,  # type: ignore[arg-type]
        retriever,  # type: ignore[arg-type]
        AgentSettings(_env_file=None),
    )
    result = build_workflow(nodes).invoke({"user_query": query})
    return result, fh_genie, retriever


def test_generate_fix_route_uses_only_verified_actionable_evidence() -> None:
    context = {"matched_issues": [matched_issue(0.91, actionable=True)], "related_issues": []}

    result, fh_genie, retriever = run_workflow(context)

    assert result["route"] == "generate_fix"
    assert result["actionable_fix_available"] is True
    assert "Enable the admission policy" in result["answer"]
    assert result["citations"] == [
        "https://github.com/kubernetes/kubernetes/pull/1"
    ]
    assert fh_genie.fix_calls == 1
    assert retriever.calls == 1


def test_generate_findings_route_never_offers_closed_without_fix() -> None:
    issue = matched_issue(
        0.88,
        actionable=False,
        resolution_type="closed_without_fix",
    )
    issue["issue_number"] = 141293
    issue["url"] = "https://github.com/kubernetes/kubernetes/issues/141293"
    context = {
        "matched_issues": [issue],
        "related_issues": [
            {
                "issue_number": 99,
                "title": "Graph-related DRA issue",
                "summary": "Shares the DRA component",
                "url": "https://github.com/kubernetes/kubernetes/issues/99",
                "shared_components": ["DRA"],
            }
        ],
    }

    result, fh_genie, retriever = run_workflow(context)

    assert result["route"] == "generate_findings"
    assert result["actionable_fix_available"] is False
    assert "no verified actionable fix" in result["answer"]
    assert "#141293" in result["answer"]
    assert "Direct semantic matches" in result["answer"]
    assert "Graph-expanded related issues (not direct semantic matches)" in result[
        "answer"
    ]
    assert "#141293" in result["answer"] and "components: DRA" in result["answer"]
    assert fh_genie.fix_calls == 0
    assert retriever.calls == 1


def test_findings_do_not_cite_unused_resolution_evidence() -> None:
    issue = matched_issue(0.88, actionable=False, resolution_type="closed_without_fix")
    issue["verified_disposition"]["evidence_urls"] = [
        "https://github.com/kubernetes/kubernetes/issues/1#issuecomment-unused"
    ]

    result, _fh_genie, _retriever = run_workflow(
        {"matched_issues": [issue], "related_issues": []}
    )

    assert result["route"] == "generate_findings"
    assert result["citations"] == [
        "https://github.com/kubernetes/kubernetes/issues/1"
    ]


def test_no_close_match_requests_logs_and_reports_no_historical_match() -> None:
    result, _fh_genie, retriever = run_workflow(
        {"matched_issues": [], "related_issues": [], "no_close_match": True}
    )

    assert result["route"] == "request_clarification"
    assert "No close historical incident was found" in result["answer"]
    assert "logs" in result["answer"]
    assert result["citations"] == []
    assert retriever.calls == 1


def test_cross_version_context_is_not_offered_as_a_solution() -> None:
    cross_version = matched_issue(0.9, actionable=True)
    cross_version["kubernetes_versions"] = ["v1.29.15"]
    cross_version["relevance"] = {
        "cross_version_match": True,
        "version_match_type": "conflict",
        "conflicting_versions": ["v1.29.15"],
    }
    result, fh_genie, _retriever = run_workflow(
        {
            "matched_issues": [],
            "cross_version_matches": [cross_version],
            "related_issues": [],
            "no_close_match": True,
        }
    )

    assert result["route"] == "request_clarification"
    assert "similar symptom exists in another Kubernetes version" in result["answer"]
    assert "must not be assumed to apply" in result["answer"]
    assert result["citations"] == []
    assert fh_genie.fix_calls == 0


def test_request_clarification_route_for_low_similarity() -> None:
    context = {"matched_issues": [matched_issue(0.2, actionable=True)], "related_issues": []}

    result, fh_genie, retriever = run_workflow(context)

    assert result["route"] == "request_clarification"
    assert result["actionable_fix_available"] is False
    assert "logs" in result["answer"]
    assert "error message" in result["answer"]
    assert result["citations"] == []
    assert fh_genie.fix_calls == 0
    assert retriever.calls == 1


def test_similarity_thresholds_are_configurable() -> None:
    settings = AgentSettings(
        _env_file=None,
        agent_relevance_threshold=0.9,
        agent_actionable_fix_threshold=0.95,
    )
    nodes = IncidentNodes(
        FakeFHGenie(),  # type: ignore[arg-type]
        FakeRetriever({}),  # type: ignore[arg-type]
        settings,
    )
    state = {
        "retrieved_context": {
            "matched_issues": [matched_issue(0.89, actionable=True)],
            "related_issues": [],
        },
        "top_similarity_score": 0.89,
    }

    assert nodes.assess_evidence(state)["route"] == "request_clarification"


def test_kubernetes_not_working_requests_clarification_without_retrieval() -> None:
    result, fh_genie, retriever = run_workflow(
        {"matched_issues": [matched_issue(0.99, actionable=True)]},
        query="Kubernetes is not working",
    )

    assert result["route"] == "request_clarification"
    assert result["has_sufficient_details"] is False
    assert "specific observed behaviour" in result["answer"]
    assert retriever.calls == 0
    assert fh_genie.fix_calls == 0


def test_generic_objects_behave_incorrectly_requests_clarification() -> None:
    result, _fh_genie, retriever = run_workflow(
        {"matched_issues": [matched_issue(0.99, actionable=True)]},
        query="Some Kubernetes objects behave incorrectly",
    )

    assert result["route"] == "request_clarification"
    assert result["has_sufficient_details"] is False
    assert "specific observed behaviour" in result["answer"]
    assert retriever.calls == 0


def test_cluster_not_working_requests_clarification_without_retrieval() -> None:
    result, _fh_genie, retriever = run_workflow(
        {"matched_issues": [matched_issue(0.99, actionable=True)]},
        query="My Kubernetes cluster is not working",
    )

    assert result["route"] == "request_clarification"
    assert result["has_sufficient_details"] is False
    assert retriever.calls == 0


def test_garbage_collector_version_query_calls_retrieval() -> None:
    result, _fh_genie, retriever = run_workflow(
        {"matched_issues": [], "related_issues": [], "no_close_match": True},
        query=(
            "The garbage collector repeatedly processes objects with valid owners "
            "on Kubernetes v1.36.2"
        ),
    )

    assert result["has_sufficient_details"] is True
    assert retriever.calls == 1
    assert retriever.query_analysis is not None
    assert retriever.query_analysis.components == ["garbage collector"]
    assert retriever.query_analysis.kubernetes_version == "v1.36.2"


def test_something_is_broken_requests_clarification_without_retrieval() -> None:
    result, _fh_genie, retriever = run_workflow(
        {"matched_issues": [matched_issue(0.99, actionable=True)]},
        query="Something is broken",
    )

    assert result["route"] == "request_clarification"
    assert retriever.calls == 0


def test_generic_error_statement_is_not_treated_as_an_error_code() -> None:
    result, _fh_genie, retriever = run_workflow(
        {"matched_issues": [matched_issue(0.99, actionable=True)]},
        query="There is an error",
    )

    assert result["route"] == "request_clarification"
    assert result["error_messages"] == []
    assert retriever.calls == 0


def test_specific_crashloop_query_calls_retrieval() -> None:
    context = {
        "matched_issues": [],
        "related_issues": [],
        "no_close_match": True,
    }
    result, _fh_genie, retriever = run_workflow(
        context,
        query="Pods enter CrashLoopBackOff after kubelet restart on Kubernetes v1.35",
    )

    assert result["has_sufficient_details"] is True
    assert result["error_messages"] == ["CrashLoopBackOff"]
    assert result["extracted_components"] == ["kubelet"]
    assert result["kubernetes_version"] == "v1.35"
    assert result["route"] == "request_clarification"
    assert "I already have error CrashLoopBackOff" in result["answer"]
    assert "component kubelet" in result["answer"]
    assert "Kubernetes version v1.35" in result["answer"]
    assert "relevant logs around the restart" in result["answer"]
    assert "pod events" in result["answer"]
    assert "container exit reason" in result["answer"]
    assert "whether one pod or multiple pods are affected" in result["answer"]
    assert "what changed immediately before the problem" in result["answer"]
    request = result["answer"].split("Please provide:", 1)[1]
    assert "exact error message" not in request
    assert "affected component" not in request
    assert "Kubernetes version" not in request
    assert result["retrieved_context"]["related_issues"] == []
    assert retriever.query_analysis is not None
    assert retriever.query_analysis.components == ["kubelet"]
    assert retriever.query_analysis.kubernetes_version == "v1.35"
    assert retriever.query_analysis.error_messages == ["CrashLoopBackOff"]
    assert retriever.query_analysis.symptom == (
        "Pods enter CrashLoopBackOff after kubelet restart"
    )
    assert retriever.calls == 1


def test_specific_query_with_high_score_but_weak_semantics_requests_clarification() -> None:
    unrelated = matched_issue(0.92, actionable=True)
    unrelated.update(
        {
            "title": "Scheduler queue latency",
            "summary": "Scheduling is delayed under load",
            "components": ["scheduler"],
            "kubernetes_versions": ["v1.34"],
        }
    )
    result, fh_genie, retriever = run_workflow(
        {"matched_issues": [unrelated], "related_issues": []}
    )

    assert result["route"] == "request_clarification"
    assert result["top_similarity_score"] == 0.92
    assert retriever.calls == 1
    assert fh_genie.fix_calls == 0
