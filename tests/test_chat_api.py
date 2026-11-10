from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.checkpointing import strict_checkpoint_serializer
from app.agent.nodes import AgentSettings, IncidentNodes
from app.agent.state import QueryUnderstanding
from app.agent.workflow import build_workflow
from app.main import AgentResources, create_app
from tests.test_incident_conversation import ConversationLLM, ConversationRetriever


class MemoryLLM(ConversationLLM):
    def __init__(self) -> None:
        super().__init__()
        self.histories: list[list[dict[str, str]] | None] = []

    def analyze_query(
        self, query: str, history: list[dict[str, str]] | None = None
    ) -> QueryUnderstanding:
        self.histories.append(history)
        if history and "same issue" in query.lower():
            # Resolve the follow-up from history, as the real prompt instructs.
            query = history[-2]["content"] + " on v1.37"
        return super().analyze_query(query)


def chat_app() -> tuple[Any, MemoryLLM]:
    llm = MemoryLLM()
    workflow = build_workflow(
        IncidentNodes(
            llm,  # type: ignore[arg-type]
            ConversationRetriever(),  # type: ignore[arg-type]
            AgentSettings(_env_file=None),
            enable_interrupts=True,
        ),
        checkpointer=InMemorySaver(serde=strict_checkpoint_serializer()),
    )
    app = create_app(lambda: AgentResources(workflow=workflow, driver=MagicMock()))
    return app, llm


GC_REPORT = (
    "The garbage collector repeatedly processes objects with valid owners "
    "on Kubernetes v1.36.2"
)


def test_first_message_creates_conversation_and_answers() -> None:
    app, llm = chat_app()
    with TestClient(app) as client:
        response = client.post("/api/chat", json={"message": GC_REPORT})
        body = response.json()
        conversations = client.get("/api/chat/conversations").json()

    assert response.status_code == 200
    assert body["message"]["role"] == "assistant"
    assert body["message"]["status"] == "completed"
    assert body["message"]["route"] == "generate_findings"
    assert "141382" in body["message"]["content"]
    assert llm.histories == [None]
    assert [item["id"] for item in conversations] == [body["conversation_id"]]
    assert conversations[0]["title"].startswith("The garbage collector")


def test_clarification_is_answered_within_the_conversation() -> None:
    app, llm = chat_app()
    with TestClient(app) as client:
        first = client.post("/api/chat", json={"message": "Kubernetes is not working"}).json()
        conversation_id = first["conversation_id"]
        second = client.post(
            "/api/chat", json={"message": GC_REPORT, "conversation_id": conversation_id}
        ).json()
        detail = client.get(f"/api/chat/conversations/{conversation_id}").json()

    assert first["message"]["status"] == "needs_clarification"
    assert second["message"]["status"] == "completed"
    assert second["message"]["route"] == "generate_findings"
    # The clarification resumed the paused thread instead of starting a new one.
    assert "Clarification 1:" in llm.analysis_inputs[-1]
    assert [message["role"] for message in detail["messages"]] == [
        "user", "assistant", "user", "assistant",
    ]


def test_follow_up_receives_conversation_history() -> None:
    app, llm = chat_app()
    with TestClient(app) as client:
        first = client.post("/api/chat", json={"message": GC_REPORT}).json()
        follow_up = client.post(
            "/api/chat",
            json={
                "message": "Does the same issue happen there?",
                "conversation_id": first["conversation_id"],
            },
        ).json()

    history = llm.histories[-1]
    assert history is not None
    assert [item["role"] for item in history] == ["user", "assistant"]
    assert history[0]["content"] == GC_REPORT
    assert follow_up["message"]["route"] == "generate_findings"


def test_unknown_conversation_and_delete() -> None:
    app, _llm = chat_app()
    with TestClient(app) as client:
        missing = client.post(
            "/api/chat", json={"message": "hello", "conversation_id": "missing"}
        )
        created = client.post("/api/chat", json={"message": GC_REPORT}).json()
        deleted = client.delete(f"/api/chat/conversations/{created['conversation_id']}")
        after = client.get(f"/api/chat/conversations/{created['conversation_id']}")
        listing = client.get("/api/chat/conversations").json()

    assert missing.status_code == 404
    assert deleted.status_code == 204
    assert after.status_code == 404
    assert listing == []


def test_invalid_chat_payloads_are_rejected() -> None:
    app, _llm = chat_app()
    with TestClient(app) as client:
        blank = client.post("/api/chat", json={"message": "   "})
        bad_id = client.post(
            "/api/chat", json={"message": "hi", "conversation_id": "../etc"}
        )

    assert blank.status_code == 422
    assert bad_id.status_code == 422
