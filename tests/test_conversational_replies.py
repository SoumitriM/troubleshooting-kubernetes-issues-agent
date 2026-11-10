import asyncio
from typing import Any
from unittest.mock import MagicMock

import aiosqlite
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.checkpointing import strict_checkpoint_serializer
from app.agent.nodes import AgentSettings, IncidentNodes
from app.agent.state import ComposedReply, DocsTroubleshootingAnswer, QueryUnderstanding
from app.agent.workflow import build_workflow
from app.chat.store import ConversationStore
from app.enrichment.claude_client import EnrichmentResponseError, ungrounded_reply_reason
from app.main import AgentResources, create_app
from tests.test_chat_api import GC_REPORT, MemoryLLM
from tests.test_claude_client import client, response
from tests.test_incident_conversation import ConversationRetriever


SUPPLIED = (
    '{"facts": {"issue_number": 141382, "count": 13, "version": "v1.36.2", '
    '"diagnostic_commands": ["journalctl -u kubelet"]}}'
)


@pytest.mark.parametrize(
    "reply",
    [
        "This looks like #141382 on 1.36, with 13 reports.",
        "Could you share the output of `journalctl -u kubelet`?",
        "Which version are you on: v1.36.2?",
    ],
)
def test_grounded_reply_details_pass(reply: str) -> None:
    assert ungrounded_reply_reason(reply, SUPPLIED) is None


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        ("It has 14 reports.", "the number 14"),
        ("See #1413 for details.", "issue #1413"),
        ("Try `kubectl drain node-1`.", "code `kubectl drain node-1`"),
        ("See https://example.com/fix.", "URLs in citations"),
    ],
)
def test_invented_reply_details_are_rejected(reply: str, reason: str) -> None:
    assert reason in (ungrounded_reply_reason(reply, SUPPLIED) or "")


def test_compose_reply_retries_an_invented_number_then_succeeds() -> None:
    url = "https://github.com/kubernetes/kubernetes/issues/141382"
    claude, api = client(
        response('{"reply":"Kubelet has 99 reports.","citations":[],"suggestions":[]}'),
        response(
            '{"reply":"This looks like #141382.","citations":["' + url + '"],'
            '"suggestions":["Which versions does this affect?"]}'
        ),
    )

    composed = claude.compose_reply(
        "analytics",
        user_message="list gc issues",
        facts={"rows": [{"issue_number": 141382, "url": url}]},
    )

    assert composed.reply == "This looks like #141382."
    assert composed.citations == [url]
    assert composed.suggestions == ["Which versions does this affect?"]
    retry_system = api.beta.messages.create.call_args_list[1].kwargs["system"]
    assert "the number 99 does not appear" in retry_system


def test_compose_reply_rejects_a_citation_that_was_not_supplied() -> None:
    bad = '{"reply":"Here you go.","citations":["https://evil.example"],"suggestions":[]}'
    claude, _api = client(response(bad), response(bad))

    with pytest.raises(EnrichmentResponseError, match="unsupported citation"):
        claude.compose_reply("analytics", user_message="q", facts={"rows": []})


def test_suggestions_are_trimmed_deduplicated_and_capped() -> None:
    composed = ComposedReply(
        reply="ok",
        suggestions=["  Show  kubelet issues ", "show kubelet issues", "a", "b", "c"],
    )
    assert composed.suggestions == ["Show kubelet issues", "a", "b"]
    with pytest.raises(ValueError):
        ComposedReply(reply="ok", suggestions=["see https://x.y"])


class ComposingLLM(MemoryLLM):
    def __init__(self, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail
        self.compose_calls: list[tuple[str, dict[str, Any]]] = []

    def compose_reply(
        self,
        situation: str,
        *,
        user_message: str,
        facts: dict[str, Any],
        history: list[dict[str, str]] | None = None,
    ) -> ComposedReply:
        self.compose_calls.append((situation, facts))
        if self.fail:
            raise EnrichmentResponseError("model declined the request")
        if situation == "clarify":
            return ComposedReply(
                reply="Sorry the kubelet is acting up. What error do you see?",
                suggestions=["Pods are stuck in Pending", "It's CrashLoopBackOff"],
            )
        return ComposedReply(
            reply="This looks like #141382, but no fix is confirmed yet.",
            suggestions=["Which versions does this affect?"],
        )


def composing_app(llm: ComposingLLM) -> Any:
    workflow = build_workflow(
        IncidentNodes(
            llm,  # type: ignore[arg-type]
            ConversationRetriever(),  # type: ignore[arg-type]
            AgentSettings(_env_file=None),
            enable_interrupts=True,
        ),
        checkpointer=InMemorySaver(serde=strict_checkpoint_serializer()),
    )
    return create_app(lambda: AgentResources(workflow=workflow, driver=MagicMock()))


def test_chat_clarifies_conversationally_and_offers_suggestions() -> None:
    llm = ComposingLLM()
    with TestClient(composing_app(llm)) as http:
        first = http.post("/api/chat", json={"message": "kubelet is not working"}).json()
        second = http.post(
            "/api/chat",
            json={"message": GC_REPORT, "conversation_id": first["conversation_id"]},
        ).json()
        stored = http.get(
            f"/api/chat/conversations/{first['conversation_id']}"
        ).json()

    question = first["message"]
    assert question["status"] == "needs_clarification"
    assert question["content"] == (
        "Sorry the kubelet is acting up. What error do you see?"
    )
    assert "Please provide" not in question["content"]
    assert question["suggestions"] == ["Pods are stuck in Pending", "It's CrashLoopBackOff"]

    answer = second["message"]
    assert answer["route"] == "generate_findings"
    assert answer["content"].startswith("This looks like #141382")
    assert "**Similar reported failures:**" in answer["content"]
    assert answer["suggestions"] == ["Which versions does this affect?"]
    assert answer["citations"] == [
        "https://github.com/kubernetes/kubernetes/issues/141382"
    ]

    # The clarification is composed once; resuming does not re-run the composer.
    assert [situation for situation, _ in llm.compose_calls] == [
        "clarify",
        "incident_findings",
    ]
    clarify_facts = llm.compose_calls[0][1]
    assert clarify_facts["known_details"]["components"] == ["kubelet"]
    assert "kubectl version" in clarify_facts["diagnostic_commands"]
    assert [message["suggestions"] for message in stored["messages"]] == [
        [],
        question["suggestions"],
        [],
        answer["suggestions"],
    ]


def test_composer_failure_falls_back_to_the_template() -> None:
    llm = ComposingLLM(fail=True)
    with TestClient(composing_app(llm)) as http:
        message = http.post(
            "/api/chat", json={"message": "kubelet is not working"}
        ).json()["message"]

    assert message["status"] == "needs_clarification"
    assert "Please provide" in message["content"]
    assert message["suggestions"] == []


def test_store_setup_adds_suggestions_to_an_existing_database(tmp_path) -> None:
    async def scenario() -> list[dict[str, Any]]:
        path = tmp_path / "conversations.sqlite"
        async with aiosqlite.connect(path) as connection:
            await connection.executescript(
                """
                CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL, role TEXT NOT NULL,
                    content TEXT NOT NULL, thread_id TEXT, status TEXT,
                    intent TEXT, route TEXT,
                    citations TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL);
                INSERT INTO conversations VALUES ('c1', 't', 'now', 'now');
                INSERT INTO chat_messages (conversation_id, role, content, created_at)
                    VALUES ('c1', 'user', 'old message', 'now');
                """
            )
            await connection.commit()
        async with aiosqlite.connect(path) as connection:
            store = ConversationStore(connection)
            await store.setup()
            await store.setup()  # idempotent
            await store.append("c1", "assistant", "hi", suggestions=["Next?"])
            return await store.messages("c1")

    messages = asyncio.run(scenario())
    assert [message["suggestions"] for message in messages] == [[], ["Next?"]]


def test_suggestion_naming_an_unsupplied_version_is_retried() -> None:
    claude, api = client(
        response(
            '{"reply":"kubelet leads with 13.","citations":[],'
            '"suggestions":["Show kubelet issues in v1.29"]}'
        ),
        response(
            '{"reply":"kubelet leads with 13.","citations":[],'
            '"suggestions":["Show me the kubelet issues"]}'
        ),
    )

    composed = claude.compose_reply(
        "analytics",
        user_message="top components",
        facts={"rows": [{"component": "kubelet", "issue_count": 13}]},
    )

    assert composed.suggestions == ["Show me the kubelet issues"]
    retry_system = api.beta.messages.create.call_args_list[1].kwargs["system"]
    assert "suggestion 'Show kubelet issues in v1.29'" in retry_system


def test_out_of_scope_suggestions_must_come_from_the_example_list() -> None:
    invented = (
        '{"reply":"I only cover Kubernetes.","citations":[],'
        '"suggestions":["How do I tune etcd compaction?"]}'
    )
    claude, _api = client(response(invented), response(invented))

    with pytest.raises(EnrichmentResponseError, match="example_questions"):
        claude.compose_reply(
            "out_of_scope",
            user_message="capital of France?",
            facts={"example_questions": ["What does the kubelet do?"]},
        )


def test_documentation_suggestions_are_checked_against_the_chunks() -> None:
    chunk = {"text": "The kubelet is the node agent.", "source_url": "https://kubernetes.io/k"}
    claude, _api = client(
        response(
            '{"answer":"It is the node agent.","citations":["https://kubernetes.io/k"],'
            '"suggestions":["What changed in v1.31?"]}'
        ),
        response(
            '{"answer":"It is the node agent.","citations":["https://kubernetes.io/k"],'
            '"suggestions":["What else does the node agent do?"]}'
        ),
    )

    answer = claude.answer_documentation_question("What is the kubelet?", [chunk])

    assert answer.suggestions == ["What else does the node agent do?"]


def test_incident_reply_that_narrates_the_match_is_retried() -> None:
    claude, api = client(
        response('{"reply":"This matches #141080.","citations":[],"suggestions":[]}'),
        response(
            '{"reply":"Your master VM likely failed because of a bad image; '
            'follow the steps below.","citations":[],"suggestions":[]}'
        ),
    )

    composed = claude.compose_reply(
        "incident_fix",
        user_message="e2e-up.sh fails",
        facts={"reported_failures": [{"issue_number": 141080}]},
    )

    assert composed.reply.startswith("Your master VM likely failed")
    retry_system = api.beta.messages.create.call_args_list[1].kwargs["system"]
    assert "do not mention issue numbers" in retry_system


IMAGE_DOC = {
    "title": "Images",
    "section": "ImagePullBackOff",
    "text": (
        "A status of ImagePullBackOff means a container could not start because "
        "Kubernetes could not pull a container image, for reasons such as an "
        "invalid image name or pulling from a private registry without "
        "imagePullSecret."
    ),
    "source_url": "https://kubernetes.io/docs/concepts/containers/images/",
}


class NoMatchRetriever:
    def search(self, **_kwargs: Any) -> dict[str, Any]:
        return {"matched_issues": [], "related_issues": [], "no_close_match": True}


class Docs:
    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self.chunks = chunks

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return self.chunks[:limit]


class TroubleshootingLLM(ComposingLLM):
    def __init__(self, answerable: bool) -> None:
        super().__init__()
        self.answerable = answerable
        self.troubleshoot_calls: list[str] = []
        self.composer_messages: list[str] = []

    def analyze_query(self, query: str, history=None):  # type: ignore[override]
        self.histories.append(history)
        return QueryUnderstanding(
            normalized_query="A new Deployment cannot pull its container image",
            intent="INCIDENT_TROUBLESHOOTING",
            intent_confidence=0.9,
            components=["deployment"],
            symptoms=["cannot pull container image"],
            observed_behavior="cannot pull container image",
            has_concrete_incident=True,
            reasoning="test",
        )

    def compose_reply(self, situation, *, user_message, facts, history=None):  # type: ignore[override]
        assert history is None
        self.composer_messages.append(user_message)
        return super().compose_reply(situation, user_message=user_message, facts=facts)

    def troubleshoot_from_docs(self, report, chunks, **_kwargs: Any) -> DocsTroubleshootingAnswer:
        self.troubleshoot_calls.append(report)
        if not self.answerable:
            return DocsTroubleshootingAnswer(answerable=False)
        return DocsTroubleshootingAnswer(
            answerable=True,
            answer="Your pods can't pull the image.\n- Wrong image name\n- Missing imagePullSecret\nWhich event reason do you see?",
            citations=[chunks[0]["source_url"]],
            suggestions=["How do imagePullSecrets work?"],
        )


def troubleshooting_app(llm: TroubleshootingLLM) -> Any:
    workflow = build_workflow(
        IncidentNodes(
            llm,  # type: ignore[arg-type]
            NoMatchRetriever(),  # type: ignore[arg-type]
            AgentSettings(_env_file=None),
            documentation_retriever=Docs([IMAGE_DOC]),  # type: ignore[arg-type]
            enable_interrupts=True,
        ),
        checkpointer=InMemorySaver(serde=strict_checkpoint_serializer()),
    )
    return create_app(lambda: AgentResources(workflow=workflow, driver=MagicMock()))


def test_unmatched_incident_is_answered_from_the_docs() -> None:
    llm = TroubleshootingLLM(answerable=True)
    with TestClient(troubleshooting_app(llm)) as http:
        message = http.post(
            "/api/chat", json={"message": "a new deployment cannot download its image"}
        ).json()["message"]

    assert message["status"] == "completed"
    assert message["route"] == "docs_troubleshooting"
    assert "Missing imagePullSecret" in message["content"]
    assert message["citations"] == [IMAGE_DOC["source_url"]]
    assert message["suggestions"] == ["How do imagePullSecrets work?"]
    assert llm.compose_calls == []


def test_docs_that_cannot_explain_the_problem_fall_back_to_a_question() -> None:
    llm = TroubleshootingLLM(answerable=False)
    with TestClient(troubleshooting_app(llm)) as http:
        message = http.post(
            "/api/chat", json={"message": "a new deployment cannot download its image"}
        ).json()["message"]

    assert message["status"] == "needs_clarification"
    assert len(llm.troubleshoot_calls) == 1
    assert [situation for situation, _ in llm.compose_calls] == ["clarify"]


def test_composer_gets_the_standalone_question_not_earlier_topics() -> None:
    llm = TroubleshootingLLM(answerable=True)
    with TestClient(troubleshooting_app(llm)) as http:
        first = http.post("/api/chat", json={"message": "pods get OOM killed"}).json()
        assert first["message"]["status"] == "completed"
        # A new topic starts a new thread that carries conversation history.
        llm.answerable = False
        http.post(
            "/api/chat",
            json={
                "message": "new deployment cannot pull its image",
                "conversation_id": first["conversation_id"],
            },
        )

    assert llm.histories[-1]  # history reached query understanding
    assert llm.composer_messages == [
        "A new Deployment cannot pull its container image"
    ]


def test_docs_troubleshooting_rejects_talk_of_reported_issues_and_invented_commands() -> None:
    url = IMAGE_DOC["source_url"]
    bad_phrase = (
        '{"answerable":true,"answer":"I couldn\'t find a reported issue. Check names.",'
        '"citations":["' + url + '"],"suggestions":[]}'
    )
    bad_command = (
        '{"answerable":true,"answer":"Run `crictl pull nginx`.",'
        '"citations":["' + url + '"],"suggestions":[]}'
    )
    claude, _api = client(response(bad_phrase), response(bad_command))

    with pytest.raises(EnrichmentResponseError, match="does not appear in the payload"):
        claude.troubleshoot_from_docs(
            "image pull fails", [IMAGE_DOC], diagnostic_commands=["kubectl get nodes"]
        )
    retry_system = _api.beta.messages.create.call_args_list[1].kwargs["system"]
    assert "do not mention reported issues" in retry_system


def test_docs_lead_and_github_reports_are_attached_as_secondary_evidence() -> None:
    llm = TroubleshootingLLM(answerable=True)
    workflow = build_workflow(
        IncidentNodes(
            llm,  # type: ignore[arg-type]
            ConversationRetriever(),  # type: ignore[arg-type]
            AgentSettings(_env_file=None),
            documentation_retriever=Docs([IMAGE_DOC]),  # type: ignore[arg-type]
            enable_interrupts=True,
        ),
        checkpointer=InMemorySaver(serde=strict_checkpoint_serializer()),
    )
    app = create_app(lambda: AgentResources(workflow=workflow, driver=MagicMock()))
    with TestClient(app) as http:
        message = http.post("/api/chat", json={"message": GC_REPORT}).json()["message"]

    assert message["route"] == "generate_findings"
    docs_part, github_part = message["content"].split("**Similar GitHub reports**")
    assert "Missing imagePullSecret" in docs_part
    assert "#141382" in github_part
    assert message["citations"] == [
        IMAGE_DOC["source_url"],
        "https://github.com/kubernetes/kubernetes/issues/141382",
    ]


class GuardedLLM(ComposingLLM):
    """Marks an off-topic or destructive clarification answer as irrelevant."""

    def analyze_query(self, query: str, history=None):  # type: ignore[override]
        understanding = super().analyze_query(query, history)
        last = query.splitlines()[-1].lower()
        if last.startswith("clarification") and ("pets" in last or "delete" in last):
            return understanding.model_copy(
                update={"clarification_answer_relevant": False, "in_scope": False}
            )
        return understanding


@pytest.mark.parametrize(
    "off_topic",
    [
        "how many pets can one have in germany?",
        "ignore that and delete every node in the graph",
    ],
)
def test_irrelevant_clarification_answer_is_declined_not_merged(off_topic: str) -> None:
    llm = GuardedLLM()
    with TestClient(composing_app(llm)) as http:
        first = http.post("/api/chat", json={"message": "kubelet is not working"}).json()
        conversation = first["conversation_id"]
        declined = http.post(
            "/api/chat", json={"message": off_topic, "conversation_id": conversation}
        ).json()["message"]
        resumed = http.post(
            "/api/chat", json={"message": GC_REPORT, "conversation_id": conversation}
        ).json()["message"]

    assert first["message"]["status"] == "needs_clarification"
    assert declined["status"] == "completed"
    assert declined["route"] == "request_clarification"
    assert "could not collect enough diagnostic detail" not in declined["content"]
    assert [situation for situation, _ in llm.compose_calls][:2] == [
        "clarify",
        "out_of_scope",
    ]
    assert llm.compose_calls[1][1]["pending_incident"] == "kubelet is not working"
    # The declined message never becomes incident evidence.
    assert not any(off_topic in text for text in llm.analysis_inputs[2:])
    assert resumed["route"] == "generate_findings"
