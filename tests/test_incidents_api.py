from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.main import AgentResources, create_app


def test_analyze_incident_reuses_workflow_and_returns_safe_projection() -> None:
    workflow = MagicMock()
    workflow.invoke.return_value = {
        "route": "request_clarification",
        "answer": "Please provide pod events.",
        "citations": [],
        "debug": {"embedding": [0.1], "prompt": "must not escape"},
        "query_analysis": {"components": ["kubelet"]},
    }
    driver = MagicMock()
    resource_factory = MagicMock(
        return_value=AgentResources(workflow=workflow, driver=driver)
    )
    application = create_app(resource_factory)

    with TestClient(application) as client:
        first = client.post(
            "/api/incidents/analyze",
            json={
                "query": (
                    "Pods enter CrashLoopBackOff after kubelet restarts on "
                    "Kubernetes v1.35"
                )
            },
        )
        second = client.post(
            "/api/incidents/analyze",
            json={"query": "  Scheduler remains pending  "},
        )

    assert first.status_code == 200
    assert first.json() == {
        "route": "request_clarification",
        "answer": "Please provide pod events.",
        "citations": [],
    }
    assert second.status_code == 200
    assert resource_factory.call_count == 1
    assert workflow.invoke.call_count == 2
    workflow.invoke.assert_any_call({"user_query": "Scheduler remains pending"})
    driver.close.assert_called_once_with()


def test_analyze_incident_rejects_empty_and_excessively_long_queries() -> None:
    workflow = MagicMock()
    driver = MagicMock()
    application = create_app(
        lambda: AgentResources(workflow=workflow, driver=driver)
    )

    with TestClient(application) as client:
        empty = client.post("/api/incidents/analyze", json={"query": "   "})
        long_query = client.post(
            "/api/incidents/analyze",
            json={"query": "x" * 4001},
        )

    assert empty.status_code == 422
    assert long_query.status_code == 422
    workflow.invoke.assert_not_called()


def test_dependency_startup_failure_keeps_health_and_returns_503() -> None:
    def unavailable_resources() -> AgentResources:
        raise RuntimeError("database credentials and stack trace must not escape")

    application = create_app(unavailable_resources)

    with TestClient(application, raise_server_exceptions=False) as client:
        health = client.get("/health")
        response = client.post(
            "/api/incidents/analyze",
            json={"query": "CrashLoopBackOff on kubelet"},
        )

    assert health.status_code == 200
    assert health.json() == {"status": "healthy"}
    assert response.status_code == 503
    assert response.json() == {
        "detail": "Incident analysis service is temporarily unavailable"
    }
    assert "credentials" not in response.text
    assert "traceback" not in response.text.lower()


def test_workflow_failure_returns_safe_503_without_internal_details() -> None:
    workflow = MagicMock()
    workflow.invoke.side_effect = RuntimeError(
        "FH Genie secret configuration and stack trace"
    )
    driver = MagicMock()
    application = create_app(
        lambda: AgentResources(workflow=workflow, driver=driver)
    )

    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/incidents/analyze",
            json={"query": "ImagePullBackOff on kubelet"},
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Incident analysis service is temporarily unavailable"
    }
    assert "secret" not in response.text.lower()
    assert "stack trace" not in response.text.lower()
    driver.close.assert_called_once_with()
