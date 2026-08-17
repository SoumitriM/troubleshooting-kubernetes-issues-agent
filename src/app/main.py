from fastapi import FastAPI

app = FastAPI(title="AI Incident Resolution Platform")


@app.get("/health")
def health() -> dict[str, str]:
    """Report whether the API process is healthy."""
    return {"status": "healthy"}
