from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app.main import AgentResources, create_app


def test_knowledge_endpoint_exposes_intent_and_safe_result() -> None:
    workflow = MagicMock()
    workflow.aget_state = AsyncMock(return_value=SimpleNamespace(values={}))
    workflow.ainvoke = AsyncMock(
        return_value={
            "intent": "GENERAL_KUBERNETES_QUESTION",
            "route": "general_answer",
            "answer": "Documentation retrieval is not configured.",
            "citations": [],
        }
    )
    app = create_app(
        lambda: AgentResources(workflow=workflow, driver=MagicMock())
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/knowledge/query",
            json={"query": "What does kubelet do?", "thread_id": "knowledge-1"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "thread_id": "knowledge-1",
        "status": "completed",
        "intent": "GENERAL_KUBERNETES_QUESTION",
        "route": "general_answer",
        "answer": "Documentation retrieval is not configured.",
        "citations": [],
    }


def test_knowledge_clarification_can_resume_same_thread() -> None:
    workflow = MagicMock()
    workflow.aget_state = AsyncMock(
        return_value=SimpleNamespace(values={"intent": "NEEDS_CLARIFICATION"}, next=("request_clarification",))
    )
    workflow.ainvoke = AsyncMock(
        return_value={
            "intent": "INCIDENT_TROUBLESHOOTING",
            "route": "generate_findings",
            "answer": "Found a related kubelet incident.",
            "citations": ["https://github.com/kubernetes/kubernetes/issues/1"],
        }
    )
    app = create_app(
        lambda: AgentResources(workflow=workflow, driver=MagicMock())
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/knowledge/knowledge-2/resume",
            json={"answer": "kubelet crashes on v1.35"},
        )

    assert response.status_code == 200
    assert response.json()["intent"] == "INCIDENT_TROUBLESHOOTING"
    assert response.json()["route"] == "generate_findings"
    assert workflow.ainvoke.await_count == 1
