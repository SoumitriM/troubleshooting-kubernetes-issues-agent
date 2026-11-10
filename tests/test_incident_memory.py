import asyncio
from typing import Any
from unittest.mock import MagicMock

import aiosqlite
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.checkpointing import strict_checkpoint_serializer
from app.agent.nodes import AgentSettings, IncidentNodes
from app.agent.state import IncidentMemory, QueryUnderstanding
from app.agent.workflow import build_workflow
from app.chat.store import ConversationStore
from app.enrichment.claude_client import EnrichmentResponseError
from app.main import AgentResources, create_app
from tests.test_chat_api import GC_REPORT
from tests.test_claude_client import client, response
from tests.test_incident_conversation import ConversationLLM, ConversationRetriever


class RememberingLLM(ConversationLLM):
    def __init__(self) -> None:
        super().__init__()
        self.memories: list[dict[str, Any] | None] = []
        self.updates: list[tuple[dict[str, Any] | None, str]] = []

    def analyze_query(  # type: ignore[override]
        self,
        query: str,
        history: list[dict[str, str]] | None = None,
        memory: dict[str, Any] | None = None,
    ) -> QueryUnderstanding:
        self.memories.append(memory)
        return super().analyze_query(query)

    def update_incident_memory(
        self, previous: dict[str, Any] | None, user_message: str, assistant_reply: str
    ) -> IncidentMemory:
        self.updates.append((previous, user_message))
        merged = IncidentMemory(**(previous or {}))
        if "v1.36.2" in user_message:
            merged.versions = [*merged.versions, "v1.36.2"]
            merged.problem = "garbage collector reprocesses objects with valid owners"
        return merged


def memory_app(llm: RememberingLLM) -> Any:
    workflow = build_workflow(
        IncidentNodes(
            llm,  # type: ignore[arg-type]
            ConversationRetriever(),  # type: ignore[arg-type]
            AgentSettings(_env_file=None),
            enable_interrupts=True,
        ),
        checkpointer=InMemorySaver(serde=strict_checkpoint_serializer()),
    )
    return create_app(
        lambda: AgentResources(workflow=workflow, driver=MagicMock(), summarizer=llm)
    )


def test_summary_from_earlier_turns_reaches_query_understanding() -> None:
    llm = RememberingLLM()
    with TestClient(memory_app(llm)) as http:
        first = http.post("/api/chat", json={"message": GC_REPORT}).json()
        http.post(
            "/api/chat",
            json={
                "message": "is there a fix for the garbage collector loop?",
                "conversation_id": first["conversation_id"],
            },
        )

    # Turn one had no summary; turn two received the one built from turn one.
    assert llm.memories[0] is None
    assert llm.memories[1] == {
        "problem": "garbage collector reprocesses objects with valid owners",
        "versions": ["v1.36.2"],
    }
    assert [message for _, message in llm.updates] == [
        GC_REPORT,
        "is there a fix for the garbage collector loop?",
    ]


def test_failed_summary_update_keeps_the_previous_summary() -> None:
    class FailingSummarizer(RememberingLLM):
        def update_incident_memory(self, previous, user_message, assistant_reply):  # type: ignore[override]
            if self.updates:
                raise EnrichmentResponseError("model declined the request")
            return super().update_incident_memory(previous, user_message, assistant_reply)

    llm = FailingSummarizer()
    with TestClient(memory_app(llm)) as http:
        first = http.post("/api/chat", json={"message": GC_REPORT}).json()
        for text in ["and on another cluster?", "any update?"]:
            http.post(
                "/api/chat",
                json={"message": text, "conversation_id": first["conversation_id"]},
            )

    assert llm.memories[2] == llm.memories[1]


def test_memory_update_keeps_only_details_from_the_turn() -> None:
    good = (
        '{"problem":"kubelet crashes after upgrade","versions":["v1.35"],'
        '"error_messages":["exit code 255"],"components":["kubelet"]}'
    )
    claude, _api = client(response(good))
    memory = claude.update_incident_memory(
        {}, "kubelet crashes after upgrade to v1.35 with exit code 255", "What does the log say?"
    )
    assert memory.versions == ["v1.35"]

    invented = '{"problem":"kubelet crashes","versions":["v1.29"]}'
    claude, _api = client(response(invented), response(invented))
    with pytest.raises(EnrichmentResponseError, match="1.29"):
        claude.update_incident_memory({}, "kubelet crashes on v1.35", "ok")

    paraphrased = '{"error_messages":["exit status 255"]}'
    claude, _api = client(response(paraphrased), response(paraphrased))
    with pytest.raises(EnrichmentResponseError, match="not verbatim"):
        claude.update_incident_memory({}, "it fails with exit code 255", "ok")


def test_store_adds_memory_to_an_existing_database(tmp_path) -> None:
    async def scenario() -> dict[str, Any]:
        path = tmp_path / "conversations.sqlite"
        async with aiosqlite.connect(path) as connection:
            await connection.executescript(
                """
                CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                INSERT INTO conversations VALUES ('c1', 't', 'now', 'now');
                """
            )
            await connection.commit()
        async with aiosqlite.connect(path) as connection:
            store = ConversationStore(connection)
            await store.setup()
            assert await store.memory("c1") == {}
            await store.set_memory("c1", {"versions": ["v1.35"]})
            return await store.memory("c1")

    assert asyncio.run(scenario()) == {"versions": ["v1.35"]}
