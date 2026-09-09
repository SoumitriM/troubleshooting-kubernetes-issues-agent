from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.checkpointing import strict_checkpoint_serializer
from app.agent.nodes import AgentSettings, IncidentNodes
from app.agent.state import QueryAnalysis, QueryUnderstanding
from app.agent.workflow import build_workflow
from app.enrichment.models import IssueEnrichment
from app.main import AgentResources, create_app


class ConversationFHGenie:
    def __init__(self) -> None:
        self.analysis_inputs: list[str] = []

    def enrich(self, title: str, body: str | None) -> IssueEnrichment:
        self.analysis_inputs.append(title)
        if "garbage collector" in title.lower():
            return IssueEnrichment(
                symptom="garbage collector repeatedly processes objects with valid owners",
                components=["garbage collector"],
                kubernetes_versions=["v1.36.2"],
            )
        return IssueEnrichment(symptom="Kubernetes is not working", components=["kubelet"])

    def analyze_query(self, query: str) -> QueryUnderstanding:
        facts = self.enrich(query, None)
        concrete = "garbage collector" in query.lower()
        components = list(facts.components)
        if concrete and "kubelet" in query.lower():
            components.insert(0, "kubelet")
        return QueryUnderstanding(
            normalized_query=query,
            intent=("INCIDENT_TROUBLESHOOTING" if concrete else "NEEDS_CLARIFICATION"),
            intent_confidence=0.95,
            components=components,
            versions=facts.kubernetes_versions,
            symptoms=[facts.symptom] if facts.symptom and concrete else [],
            observed_behavior=facts.symptom if concrete else None,
            has_concrete_incident=concrete,
            reasoning="Test semantic analysis",
        )


class ConversationRetriever:
    def __init__(self) -> None:
        self.calls: list[tuple[str, QueryAnalysis]] = []

    def search(
        self,
        *,
        query_text: str,
        query_analysis: QueryAnalysis,
        limit: int,
        debug: bool,
    ) -> dict[str, Any]:
        self.calls.append((query_text, query_analysis))
        return {
            "matched_issues": [
                {
                    "issue_number": 141382,
                    "title": "Garbage collector processes valid owners",
                    "summary": "Objects with valid owners are repeatedly processed",
                    "symptom": "garbage collector repeatedly processes objects with valid owners",
                    "url": "https://github.com/kubernetes/kubernetes/issues/141382",
                    "score": 0.91,
                    "components": ["garbage collector"],
                    "relevance": {"accepted": True},
                }
            ],
            "related_issues": [],
        }


def conversation_app() -> tuple[Any, ConversationFHGenie, ConversationRetriever]:
    fh_genie = ConversationFHGenie()
    retriever = ConversationRetriever()
    saver = InMemorySaver(serde=strict_checkpoint_serializer())
    workflow = build_workflow(
        IncidentNodes(
            fh_genie,  # type: ignore[arg-type]
            retriever,  # type: ignore[arg-type]
            AgentSettings(_env_file=None),
            enable_interrupts=True,
        ),
        checkpointer=saver,
    )
    app = create_app(lambda: AgentResources(workflow=workflow, driver=MagicMock()))
    return app, fh_genie, retriever


def test_pause_resume_merges_analysis_and_completes() -> None:
    app, fh_genie, retriever = conversation_app()
    with TestClient(app) as client:
        paused = client.post(
            "/api/incidents/analyze",
            json={"query": "kubelet is not working", "thread_id": "merge-1"},
        )
        resumed = client.post(
            "/api/incidents/merge-1/resume",
            json={
                "answer": (
                    "The garbage collector repeatedly processes objects with valid "
                    "owners on Kubernetes v1.36.2"
                )
            },
        )

    assert paused.status_code == 200
    assert paused.json()["status"] == "needs_clarification"
    assert paused.json()["known_details"] == {"components": ["kubelet"]}
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "completed"
    assert resumed.json()["route"] == "generate_findings"
    assert len(fh_genie.analysis_inputs) == 2
    assert "Clarification 1:" in fh_genie.analysis_inputs[1]
    assert retriever.calls
    analysis = retriever.calls[0][1]
    assert analysis.components == ["kubelet", "garbage collector"]
    assert analysis.kubernetes_version == "v1.36.2"


def test_resume_rejects_unknown_thread() -> None:
    app, _fh_genie, _retriever = conversation_app()
    with TestClient(app) as client:
        response = client.post(
            "/api/incidents/missing-thread/resume",
            json={"answer": "Pod events show an OOMKilled container"},
        )
    assert response.status_code == 404


def test_maximum_three_clarification_attempts_returns_safe_completion() -> None:
    app, _fh_genie, retriever = conversation_app()
    with TestClient(app) as client:
        response = client.post(
            "/api/incidents/analyze",
            json={"query": "Kubernetes is not working", "thread_id": "attempts-1"},
        )
        assert response.json()["status"] == "needs_clarification"
        for attempt in range(1, 4):
            response = client.post(
                "/api/incidents/attempts-1/resume",
                json={"answer": f"I do not have any more details {attempt}"},
            )
            if attempt < 3:
                assert response.json()["status"] == "needs_clarification"

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["route"] == "request_clarification"
    assert "after three clarification attempts" in response.json()["answer"]
    assert retriever.calls == []


def test_thread_and_resume_payload_validation() -> None:
    app, _fh_genie, _retriever = conversation_app()
    with TestClient(app) as client:
        invalid_thread = client.post(
            "/api/incidents/analyze",
            json={"query": "Kubernetes is not working", "thread_id": "bad/thread"},
        )
        empty_answer = client.post(
            "/api/incidents/good-thread/resume", json={"answer": "   "}
        )
    assert invalid_thread.status_code == 422
    assert empty_answer.status_code == 422
