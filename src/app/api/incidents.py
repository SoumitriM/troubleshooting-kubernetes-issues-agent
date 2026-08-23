import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool

from app.api.models import IncidentAnalyzeRequest, IncidentAnalyzeResponse


LOGGER = logging.getLogger(__name__)
router = APIRouter(prefix="/api/incidents", tags=["incidents"])


@router.post("/analyze", response_model=IncidentAnalyzeResponse)
async def analyze_incident(
    payload: IncidentAnalyzeRequest,
    request: Request,
) -> IncidentAnalyzeResponse:
    """Run the shared incident workflow and expose only its safe final output."""

    workflow = getattr(request.app.state, "incident_workflow", None)
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Incident analysis service is temporarily unavailable",
        )
    try:
        result: dict[str, Any] = await run_in_threadpool(
            workflow.invoke,
            {"user_query": payload.query},
        )
        return IncidentAnalyzeResponse.model_validate(
            {
                "route": result.get("route"),
                "answer": result.get("answer"),
                "citations": result.get("citations", []),
            }
        )
    except Exception:
        # Do not include queries, dependency details, prompts, or stack traces in
        # the HTTP response. Operational diagnostics remain server-side.
        LOGGER.error("Incident analysis request failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Incident analysis service is temporarily unavailable",
        ) from None
