# AI Incident Resolution Platform

Initial FastAPI service for an AI-assisted incident-resolution platform. It
provides a health endpoint and a GitHub issue ingestion command. It does not
yet include an LLM, database, frontend, or Kubernetes configuration.

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

`GITHUB_TOKEN` is optional for public repositories. Set it in `.env` to use
authenticated GitHub API rate limits. Never commit the `.env` file.

Run the API locally:

```bash
uvicorn app.main:app --reload
```

The health endpoint is available at `http://127.0.0.1:8000/health`.

Run the tests:

```bash
pytest
```

## GitHub issue ingestion

Fetch closed issues from a public repository and write normalized JSONL:

```bash
python -m app.ingestion.cli \
  --owner kubernetes \
  --repo kubernetes \
  --limit 100 \
  --output data/raw/kubernetes_issues.jsonl
```

The command creates missing output directories, follows API pagination, and
excludes pull requests returned by GitHub's Issues API. Each line contains
only the issue number, title, body, labels, state, timestamps, comment count,
and HTML URL. Generated files under `data/raw/` are ignored by Git.

## Project structure

```text
.
├── src/app/          # FastAPI application package
│   └── ingestion/    # GitHub issue ingestion client and CLI
├── tests/            # Automated tests
├── .env.example      # Local configuration template
└── pyproject.toml    # Project metadata and dependencies
```
