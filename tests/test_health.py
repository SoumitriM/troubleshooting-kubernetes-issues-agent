from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_readiness_fails_when_dependencies_are_unavailable() -> None:
    response = client.get("/ready")

    assert response.status_code == 503
