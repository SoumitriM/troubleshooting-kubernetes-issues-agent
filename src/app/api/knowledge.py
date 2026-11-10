"""Public endpoint for the routed Kubernetes knowledge assistant."""

import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status
from langgraph.types import Command

from app.api.incidents import THREAD_ID_PATTERN
from app.api.models import (
    IncidentResumeRequest,
    KnowledgeQueryRequest,
    KnowledgeQueryResponse,
)


LOGGER = logging.getLogger(__name__)
router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


@router.post("/query", response_model=KnowledgeQueryResponse)
async def query_knowledge(
    payload: KnowledgeQueryRequest, request: Request
) -> KnowledgeQueryResponse:
    workflow = getattr(request.app.state, "incident_workflow", None)
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Kubernetes knowledge service is temporarily unavailable",
        )
    thread_id = payload.thread_id or str(uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    try:
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
        interruptions = result.get("__interrupt__") or ()
        if interruptions:
            return _clarification_response(thread_id, result)
        return KnowledgeQueryResponse(
            thread_id=thread_id,
            status="completed",
            intent=result["intent"],
            route=result["route"],
            answer=result["answer"],
            citations=result.get("citations", []),
            suggestions=result.get("suggestions", []),
        )
    except HTTPException:
        raise
    except Exception:
        LOGGER.exception("Kubernetes knowledge query failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Kubernetes knowledge service is temporarily unavailable",
        ) from None


@router.post("/{thread_id}/resume", response_model=KnowledgeQueryResponse)
async def resume_knowledge(
    thread_id: str, payload: IncidentResumeRequest, request: Request
) -> KnowledgeQueryResponse:
    """Continue a checkpointed knowledge query that requested clarification."""

    if not THREAD_ID_PATTERN.fullmatch(thread_id):
        raise HTTPException(status_code=422, detail="Invalid thread_id")
    workflow = getattr(request.app.state, "incident_workflow", None)
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Kubernetes knowledge service is temporarily unavailable",
        )
    config = {"configurable": {"thread_id": thread_id}}
    try:
        snapshot = await workflow.aget_state(config)
        if not snapshot.values or not snapshot.next:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation was not found or is not awaiting clarification",
            )
        result: dict[str, Any] = await workflow.ainvoke(
            Command(resume=payload.answer), config
        )
        interruptions = result.get("__interrupt__") or ()
        if interruptions:
            return _clarification_response(thread_id, result)
        return KnowledgeQueryResponse(
            thread_id=thread_id,
            status="completed",
            intent=result["intent"],
            route=result["route"],
            answer=result["answer"],
            citations=result.get("citations", []),
            suggestions=result.get("suggestions", []),
        )
    except HTTPException:
        raise
    except Exception:
        LOGGER.exception("Kubernetes knowledge clarification failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Kubernetes knowledge service is temporarily unavailable",
        ) from None


def _clarification_response(
    thread_id: str, result: dict[str, Any]
) -> KnowledgeQueryResponse:
    value = result["__interrupt__"][0].value
    return KnowledgeQueryResponse(
        thread_id=thread_id,
        status="needs_clarification",
        intent=result.get("intent", "NEEDS_CLARIFICATION"),
        route="request_clarification",
        answer=value["question"],
        citations=value.get("citations", []),
        suggestions=value.get("suggestions", []),
    )
