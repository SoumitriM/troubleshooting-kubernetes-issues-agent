from app.agent.nodes import AgentSettings, IncidentNodes
from app.documentation.models import GroundedDocumentationAnswer


CHUNK = {
    "title": "Kubernetes Components",
    "section": "Node Components",
    "text": "## Node Components\nThe kubelet is an agent that runs on each node.",
    "score": 0.91,
    "source_path": "content/en/docs/concepts/overview/components.md",
    "source_url": "https://kubernetes.io/docs/concepts/overview/components/",
}


class DocsRetriever:
    def __init__(self, results):
        self.results = results

    def search(self, query, limit=5):
        return self.results


class UnsupportedCitationModel:
    def answer_documentation_question(self, query, chunks):
        return GroundedDocumentationAnswer(
            answer="Invented answer",
            citations=["https://example.invalid/invented"],
        )


def nodes(results):
    return IncidentNodes(  # type: ignore[arg-type]
        UnsupportedCitationModel(),
        None,
        AgentSettings(_env_file=None),
        documentation_retriever=DocsRetriever(results),
    )


def test_answer_citations_are_limited_to_retrieved_urls() -> None:
    instance = nodes([CHUNK])
    state = {"normalized_query": "What does kubelet do?"}
    state.update(instance.retrieve_kubernetes_docs(state))
    answer = instance.answer_from_kubernetes_docs(state)

    assert "agent that runs on each node" in answer["answer"]
    assert answer["citations"] == [CHUNK["source_url"]]


def test_no_documentation_returns_safe_fallback() -> None:
    instance = nodes([])
    state = {"normalized_query": "What is an unknown feature?"}
    state.update(instance.retrieve_kubernetes_docs(state))
    answer = instance.answer_from_kubernetes_docs(state)

    assert "could not find relevant content" in answer["answer"]
    assert answer["citations"] == []
