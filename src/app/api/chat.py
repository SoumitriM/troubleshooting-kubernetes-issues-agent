"""Multi-turn chat endpoints with persisted conversation memory."""

import asyncio
import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status
from langgraph.types import Command

from app.api.models import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ConversationDetail,
    ConversationSummary,
)
from app.chat.store import ConversationStore


LOGGER = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])

# Earlier messages passed to query understanding to resolve follow-ups. Older
# turns are kept for display but not sent to the model.
HISTORY_MESSAGES = 6
HISTORY_MESSAGE_CHARS = 1500
UNAVAILABLE = "Chat service is temporarily unavailable"

# One turn at a time per conversation keeps history and clarification state
# consistent when a client sends messages concurrently.
_conversation_locks: dict[str, asyncio.Lock] = {}


@router.post("", response_model=ChatResponse)
async def send_message(payload: ChatRequest, request: Request) -> ChatResponse:
    workflow, store = _dependencies(request)
    conversation_id = payload.conversation_id or uuid4().hex
    lock = _conversation_locks.setdefault(conversation_id, asyncio.Lock())
    async with lock:
        try:
            if payload.conversation_id is None:
                await store.create(conversation_id, payload.message)
                previous: list[dict[str, Any]] = []
            elif await store.exists(conversation_id):
                previous = await store.messages(conversation_id)
            else:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Conversation was not found",
                )
            await store.append(conversation_id, "user", payload.message)
            thread_id, result = await _run_turn(
                workflow, conversation_id, payload.message, previous
            )
            reply = _assistant_fields(result)
            message = await store.append(
                conversation_id, "assistant", thread_id=thread_id, **reply
            )
        except HTTPException:
            raise
        except Exception:
            LOGGER.exception("Chat turn failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNAVAILABLE
            ) from None
    return ChatResponse(
        conversation_id=conversation_id, message=_public_message(message)
    )


@router.get("/conversations", response_model=list[ConversationSummary])
async def list_conversations(request: Request) -> list[ConversationSummary]:
    _, store = _dependencies(request)
    return [
        ConversationSummary(**row) for row in await store.list_conversations()
    ]


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(conversation_id: str, request: Request) -> ConversationDetail:
    _, store = _dependencies(request)
    if not await store.exists(conversation_id):
        raise HTTPException(status_code=404, detail="Conversation was not found")
    return ConversationDetail(
        id=conversation_id,
        messages=[
            _public_message(message)
            for message in await store.messages(conversation_id)
        ],
    )


@router.delete(
    "/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_conversation(conversation_id: str, request: Request) -> None:
    _, store = _dependencies(request)
    if not await store.delete(conversation_id):
        raise HTTPException(status_code=404, detail="Conversation was not found")
    _conversation_locks.pop(conversation_id, None)


async def _run_turn(
    workflow: Any,
    conversation_id: str,
    message: str,
    previous: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Answer a pending clarification, or start a new workflow thread."""

    last = previous[-1] if previous else None
    if last and last["status"] == "needs_clarification" and last["thread_id"]:
        config = {"configurable": {"thread_id": last["thread_id"]}}
        snapshot = await workflow.aget_state(config)
        if snapshot.values and snapshot.next:
            return last["thread_id"], await workflow.ainvoke(
                Command(resume=message), config
            )
    thread_id = f"{conversation_id}:{uuid4().hex[:12]}"
    history = [
        {"role": item["role"], "content": item["content"][:HISTORY_MESSAGE_CHARS]}
        for item in previous[-HISTORY_MESSAGES:]
    ]
    result = await workflow.ainvoke(
        {
            "original_query": message,
            "user_query": message,
            "clarification_history": [],
            "clarification_attempts": 0,
            "conversation_history": history,
        },
        {"configurable": {"thread_id": thread_id}},
    )
    return thread_id, result


def _assistant_fields(result: dict[str, Any]) -> dict[str, Any]:
    interruptions = result.get("__interrupt__") or ()
    if interruptions:
        return {
            "content": interruptions[0].value["question"],
            "status": "needs_clarification",
            "intent": result.get("intent", "NEEDS_CLARIFICATION"),
            "route": "request_clarification",
            "citations": [],
        }
    return {
        "content": result["answer"],
        "status": "completed",
        "intent": result.get("intent"),
        "route": result.get("route"),
        "citations": result.get("citations", []),
    }


def _public_message(message: dict[str, Any]) -> ChatMessage:
    return ChatMessage(
        role=message["role"],
        content=message["content"],
        status=message.get("status"),
        intent=message.get("intent"),
        route=message.get("route"),
        citations=message.get("citations", []),
        created_at=message["created_at"],
    )


def _dependencies(request: Request) -> tuple[Any, ConversationStore]:
    workflow = getattr(request.app.state, "incident_workflow", None)
    store = getattr(request.app.state, "conversation_store", None)
    if workflow is None or store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNAVAILABLE
        )
    return workflow, store
