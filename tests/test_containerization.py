from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_runs_fastapi_as_non_root_with_one_worker() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM python:3.11-slim" in dockerfile
    assert dockerfile.count("\nFROM ") == 0
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert "PYTHONUNBUFFERED=1" in dockerfile
    assert "USER appuser" in dockerfile
    assert '"uvicorn", "app.main:app"' in dockerfile
    assert '"--host", "0.0.0.0"' in dockerfile
    assert '"--port", "8000"' in dockerfile
    assert '"--workers", "1"' in dockerfile
    assert "/health" in dockerfile
    assert "COPY .env" not in dockerfile
    assert "COPY --from=builder /opt/venv" not in dockerfile
    assert "python -m pip install --no-cache-dir --no-compile ." in dockerfile
    assert "-type d -name __pycache__" in dockerfile
    assert "-type f -name '*.pyc' -delete" in dockerfile


def test_dockerignore_excludes_secrets_state_and_generated_files() -> None:
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    for required in {
        ".git",
        ".git*",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".env",
        ".env.*",
        "*.env*",
        "*.sqlite",
        "*.sqlite-*",
        "data",
    }:
        assert required in ignored


def test_compose_adds_single_persistent_healthy_application_service() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "neo4j_data:/data" in compose
    assert "neo4j://neo4j:7687" in compose
    assert "./data:/app/data" in compose
    assert "replicas: 1" in compose
    assert "- .env" in compose
    assert "condition: service_healthy" in compose
    assert "http://127.0.0.1:8000/ready" in compose
    assert "ANTHROPIC_API_KEY:" not in compose
    assert "frontend:" in compose
    assert '"5173:8080"' in compose
    assert "incident-resolution-ai" in compose
