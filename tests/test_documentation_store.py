import sqlite3
from pathlib import Path

from app.documentation.store import DocumentationStore


class KeywordEmbedder:
    def encode(self, texts):
        return [[1.0, 0.0] if "kubelet" in text.lower() else [0.0, 1.0] for text in texts]

    def encode_query(self, query):
        return [1.0, 0.0] if "kubelet" in query.lower() else [0.0, 1.0]


def test_index_is_idempotent_and_retrieval_keeps_provenance(tmp_path: Path) -> None:
    source = tmp_path / "website"
    document = source / "content/en/docs/concepts/overview/components.md"
    document.parent.mkdir(parents=True)
    document.write_text(
        "---\ntitle: Kubernetes Components\n---\n## Node Components\nThe kubelet is a node agent.",
        encoding="utf-8",
    )
    index = tmp_path / "docs.sqlite"
    store = DocumentationStore(index, KeywordEmbedder())  # type: ignore[arg-type]

    first = store.index_repository(source)
    second = store.index_repository(source)
    results = store.search("What does kubelet do?", 5)

    assert first["changed_chunks"] == 1
    assert second == {"total_chunks": 1, "changed_chunks": 0}
    with sqlite3.connect(index) as connection:
        assert connection.execute("SELECT count(*) FROM documentation_chunks").fetchone()[0] == 1
    assert results[0]["title"] == "Kubernetes Components"
    assert results[0]["section"] == "Node Components"
    assert results[0]["source_url"].startswith("https://kubernetes.io/docs/")
