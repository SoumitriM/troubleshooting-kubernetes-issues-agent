# AI Incident Resolution Platform

Initial FastAPI service for an AI-assisted incident-resolution platform. This
first iteration provides only a health endpoint; it does not yet include an
LLM, database, frontend, or Kubernetes configuration.

## Prerequisites

- Python 3.11

## Local setup

Create and activate a virtual environment:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

Install the application and development dependencies:

```bash
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Optionally create a local environment file:

```bash
cp .env.example .env
```

Run the API locally:

```bash
uvicorn app.main:app --reload
```

The health endpoint is available at `http://127.0.0.1:8000/health`.

Run the tests:

```bash
pytest
```

## Project structure

```text
.
├── src/app/          # FastAPI application package
├── tests/            # Automated tests
├── .env.example      # Local configuration template
└── pyproject.toml    # Project metadata and dependencies
```
