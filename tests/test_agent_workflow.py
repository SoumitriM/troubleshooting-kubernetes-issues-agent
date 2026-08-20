from typing import Any

from app.agent.nodes import AgentSettings, IncidentNodes
from app.agent.state import GroundedFixAnswer
from app.agent.workflow import build_workflow
from app.enrichment.models import IssueEnrichment


class FakeFHGenie:
    def __init__(self) -> None:
        self.fix_calls = 0

    def enrich(self, title: str, body: str | None) -> IssueEnrichment:
        assert title
        assert body is None
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

    def retrieve(self, query: str, limit: int) -> dict[str, Any]:
        assert query
        assert limit == 5
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


def run_workflow(context: dict[str, Any]) -> tuple[dict[str, Any], FakeFHGenie]:
    fh_genie = FakeFHGenie()
    nodes = IncidentNodes(
        fh_genie,  # type: ignore[arg-type]
        FakeRetriever(context),  # type: ignore[arg-type]
        AgentSettings(_env_file=None),
    )
    result = build_workflow(nodes).invoke(
        {"user_query": "Non-admin users can enable DRA adminAccess"}
    )
    return result, fh_genie


def test_generate_fix_route_uses_only_verified_actionable_evidence() -> None:
    context = {"matched_issues": [matched_issue(0.91, actionable=True)], "related_issues": []}

    result, fh_genie = run_workflow(context)

    assert result["route"] == "generate_fix"
    assert result["actionable_fix_available"] is True
    assert "Enable the admission policy" in result["answer"]
    assert result["citations"] == [
        "https://github.com/kubernetes/kubernetes/pull/1"
    ]
    assert fh_genie.fix_calls == 1


def test_generate_findings_route_never_offers_closed_without_fix() -> None:
    issue = matched_issue(
        0.88,
        actionable=False,
        resolution_type="closed_without_fix",
    )
    issue["issue_number"] = 141293
    issue["url"] = "https://github.com/kubernetes/kubernetes/issues/141293"
    context = {"matched_issues": [issue], "related_issues": []}

    result, fh_genie = run_workflow(context)

    assert result["route"] == "generate_findings"
    assert result["actionable_fix_available"] is False
    assert "no verified actionable fix" in result["answer"]
    assert "#141293" in result["answer"]
    assert fh_genie.fix_calls == 0


def test_request_clarification_route_for_low_similarity() -> None:
    context = {"matched_issues": [matched_issue(0.2, actionable=True)], "related_issues": []}

    result, fh_genie = run_workflow(context)

    assert result["route"] == "request_clarification"
    assert result["actionable_fix_available"] is False
    assert "logs" in result["answer"]
    assert "error message" in result["answer"]
    assert result["citations"] == []
    assert fh_genie.fix_calls == 0


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
