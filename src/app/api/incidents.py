import logging
import re
from uuid import uuid4
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from langgraph.types import Command

from app.api.models import (
    IncidentAnalyzeRequest,
    IncidentClarificationResponse,
    IncidentCompletedResponse,
    IncidentConversationResponse,
    IncidentResumeRequest,
)


LOGGER = logging.getLogger(__name__)
router = APIRouter(prefix="/api/incidents", tags=["incidents"])


THREAD_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


@router.post("/analyze", response_model=IncidentConversationResponse)
async def analyze_incident(
    payload: IncidentAnalyzeRequest,
    request: Request,
) -> IncidentConversationResponse:
    """Run the shared incident workflow and expose only its safe final output."""

    workflow = getattr(request.app.state, "incident_workflow", None)
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Incident analysis service is temporarily unavailable",
        )
    try:
        thread_id = payload.thread_id or str(uuid4())
        config = {"configurable": {"thread_id": thread_id}}
        existing = await workflow.aget_state(config)
        if existing.values:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Conversation thread already exists",
            )
        result: dict[str, Any] = await workflow.ainvoke(
            {
                "original_query": payload.query,
                "user_query": payload.query,
                "clarification_history": [],
                "clarification_attempts": 0,
            },
            config,
        )
        return _public_result(thread_id, result)
    except HTTPException:
        raise
    except Exception:
        # Do not include queries, dependency details, prompts, or stack traces in
        # the HTTP response. Operational diagnostics remain server-side.
        LOGGER.error("Incident analysis request failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Incident analysis service is temporarily unavailable",
        ) from None


@router.post("/{thread_id}/resume", response_model=IncidentConversationResponse)
async def resume_incident(
    thread_id: str,
    payload: IncidentResumeRequest,
    request: Request,
) -> IncidentConversationResponse:
    if not THREAD_ID_PATTERN.fullmatch(thread_id):
        raise HTTPException(status_code=422, detail="Invalid thread_id")
    workflow = getattr(request.app.state, "incident_workflow", None)
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Incident analysis service is temporarily unavailable",
        )
    config = {"configurable": {"thread_id": thread_id}}
    try:
        snapshot = await workflow.aget_state(config)
        if not snapshot.values or not snapshot.next:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation thread was not found or is not awaiting clarification",
            )
        result = await workflow.ainvoke(Command(resume=payload.answer), config)
        return _public_result(thread_id, result)
    except HTTPException:
        raise
    except Exception:
        LOGGER.error("Incident clarification resume failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Incident analysis service is temporarily unavailable",
        ) from None


def _public_result(
    thread_id: str,
    result: dict[str, Any],
) -> IncidentConversationResponse:
    interruptions = result.get("__interrupt__") or ()
    if interruptions:
        value = interruptions[0].value
        return IncidentClarificationResponse.model_validate(
            {"thread_id": thread_id, "status": "needs_clarification", **value}
        )
    return IncidentCompletedResponse.model_validate(
        {
            "thread_id": thread_id,
            "status": "completed",
            "route": result.get("route"),
            "answer": result.get("answer"),
            "citations": result.get("citations", []),
        }
    )
