from pathlib import Path

import pytest

from app.documentation.parser import (
    canonical_docs_url,
    chunk_page,
    iter_english_pages,
)


def write_doc(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_only_supported_english_documentation_is_parsed(tmp_path: Path) -> None:
    write_doc(
        tmp_path,
        "content/en/docs/concepts/overview/components.md",
        "---\ntitle: Kubernetes Components\n---\n## Node Components\nThe kubelet runs on nodes.",
    )
    write_doc(
        tmp_path,
        "content/de/docs/concepts/overview/components.md",
        "---\ntitle: Komponenten\n---\nÜbersetzte Dokumentation",
    )
    write_doc(tmp_path, "content/en/blog/post.md", "# Blog\nNot user docs")

    pages = list(iter_english_pages(tmp_path))

    assert len(pages) == 1
    assert pages[0].title == "Kubernetes Components"
    assert "title:" not in pages[0].body
    assert pages[0].source_path.startswith("content/en/docs/")


def test_canonical_url_conversion_is_deterministic() -> None:
    assert canonical_docs_url(
        "content/en/docs/concepts/overview/components.md"
    ) == "https://kubernetes.io/docs/concepts/overview/components/"
    assert canonical_docs_url(
        "content/en/docs/tasks/_index.md"
    ) == "https://kubernetes.io/docs/tasks/"
    with pytest.raises(ValueError):
        canonical_docs_url("content/de/docs/concepts/components.md")


def test_chunking_preserves_heading_metadata_and_code_blocks(tmp_path: Path) -> None:
    path = write_doc(
        tmp_path,
        "content/en/docs/tasks/run.md",
        "---\ntitle: Run Pods\n---\n## Create a Pod\nUse this command:\n\n```bash\nkubectl run demo --image=nginx\n```",
    )
    page = list(iter_english_pages(tmp_path))[0]
    chunks = chunk_page(page)

    assert path.exists()
    assert chunks[0].section == "Create a Pod"
    assert "kubectl run demo --image=nginx" in chunks[0].text
    assert chunks[0].source_url == "https://kubernetes.io/docs/tasks/run/"


def test_repeated_heading_names_get_unique_stable_chunk_ids(tmp_path: Path) -> None:
    write_doc(
        tmp_path,
        "content/en/docs/concepts/repeated.md",
        "# Repeated\n## Notes\nFirst version.\n## Notes\nSecond version.",
    )
    page = list(iter_english_pages(tmp_path))[0]

    first_pass = chunk_page(page)
    second_pass = chunk_page(page)

    assert [chunk.chunk_id for chunk in first_pass] == [
        chunk.chunk_id for chunk in second_pass
    ]
    assert len({chunk.chunk_id for chunk in first_pass}) == len(first_pass)
