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


def test_search_reloads_cached_index_after_reindexing(tmp_path: Path) -> None:
    source = tmp_path / "website"
    document = source / "content/en/docs/concepts/overview/components.md"
    document.parent.mkdir(parents=True)
    document.write_text(
        "---\ntitle: Components\n---\n## Control plane\nThe scheduler assigns Pods.",
        encoding="utf-8",
    )
    index = tmp_path / "docs.sqlite"
    store = DocumentationStore(index, KeywordEmbedder())  # type: ignore[arg-type]
    store.index_repository(source)
    assert store.search("kubelet", 5)[0]["section"] == "Control plane"

    document.write_text(
        "---\ntitle: Components\n---\n## Node\nThe kubelet is a node agent.",
        encoding="utf-8",
    )
    store.index_repository(source)
    results = store.search("kubelet", 5)

    assert [result["section"] for result in results] == ["Node"]
    assert results[0]["score"] == 1.0


def test_lexical_tokens_split_camel_case_api_names() -> None:
    from app.documentation.store import lexical_tokens

    tokens = set(lexical_tokens("What is a PersistentVolumeClaim?"))

    assert {"persistentvolumeclaim", "persistent", "volume", "claim"} <= tokens
    assert "what" not in tokens


def test_hybrid_search_caps_chunks_per_page(tmp_path: Path) -> None:
    source = tmp_path / "website"
    page = source / "content/en/docs/concepts/workloads/autoscaling.md"
    other = source / "content/en/docs/concepts/workloads/replicas.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntitle: Autoscaling\n---\n"
        + "\n".join(f"## Part {n}\nThe autoscaler changes replicas ({n})." for n in range(6)),
        encoding="utf-8",
    )
    other.write_text(
        "---\ntitle: Replicas\n---\n## Replicas\nAutoscaler targets set replicas.",
        encoding="utf-8",
    )
    store = DocumentationStore(tmp_path / "docs.sqlite", KeywordEmbedder())  # type: ignore[arg-type]
    store.index_repository(source)

    results = store.search("autoscaler replicas", 6)
    pages = [result["source_url"] for result in results]

    assert max(pages.count(url) for url in set(pages)) <= 3
    assert any(url.endswith("/replicas/") for url in pages)
