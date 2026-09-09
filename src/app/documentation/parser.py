"""Parse English kubernetes/website Markdown into semantic chunks."""

import hashlib
import re
from pathlib import Path, PurePosixPath

from app.documentation.models import DocumentationChunk, DocumentationPage


DOCS_ROOT = PurePosixPath("content/en/docs")
ALLOWED_SECTIONS = frozenset({"concepts", "tasks", "tutorials", "reference"})
FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
TITLE_FIELD = re.compile(r'^title:\s*["\']?(.*?)["\']?\s*$', re.MULTILINE)
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def canonical_docs_url(source_path: str) -> str:
    path = PurePosixPath(source_path)
    try:
        relative = path.relative_to(DOCS_ROOT)
    except ValueError as error:
        raise ValueError("source path must be under content/en/docs") from error
    parts = list(relative.parts)
    if not parts or parts[0] not in ALLOWED_SECTIONS:
        raise ValueError("source path is outside an indexed documentation section")
    filename = parts.pop()
    if filename not in {"index.md", "_index.md"}:
        if not filename.endswith(".md"):
            raise ValueError("documentation source must be Markdown")
        parts.append(filename[:-3])
    suffix = "/".join(parts)
    return f"https://kubernetes.io/docs/{suffix}/"


def parse_markdown(path: Path, repository_root: Path) -> DocumentationPage:
    source_path = path.relative_to(repository_root).as_posix()
    raw = path.read_text(encoding="utf-8")
    match = FRONT_MATTER.match(raw)
    front_matter = match.group(1) if match else ""
    body = raw[match.end():] if match else raw
    title_match = TITLE_FIELD.search(front_matter)
    first_heading = next(
        (heading.group(2) for line in body.splitlines() if (heading := HEADING.match(line))),
        None,
    )
    title = (title_match.group(1).strip() if title_match else None) or first_heading
    if not title:
        title = path.stem.replace("-", " ").replace("_", " ").title()
    return DocumentationPage(
        title=title,
        body=body.strip(),
        source_path=source_path,
        source_url=canonical_docs_url(source_path),
    )


def iter_english_pages(repository_root: Path):
    docs_root = repository_root / DOCS_ROOT
    for section in sorted(ALLOWED_SECTIONS):
        for path in sorted((docs_root / section).rglob("*.md")):
            if path.is_file():
                yield parse_markdown(path, repository_root)


def chunk_page(
    page: DocumentationPage, *, max_words: int = 750
) -> list[DocumentationChunk]:
    sections: list[tuple[str, list[str]]] = []
    heading_stack: list[tuple[int, str]] = []
    current_heading = page.title
    current_lines: list[str] = []
    in_code = False
    for line in page.body.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
        heading = None if in_code else HEADING.match(line)
        if heading:
            if any(item.strip() for item in current_lines):
                sections.append((current_heading, current_lines))
            level, name = len(heading.group(1)), heading.group(2).strip()
            heading_stack = [item for item in heading_stack if item[0] < level]
            heading_stack.append((level, name))
            current_heading = " › ".join(item[1] for item in heading_stack)
            current_lines = [line]
        else:
            current_lines.append(line)
    if any(item.strip() for item in current_lines):
        sections.append((current_heading, current_lines))

    chunks: list[DocumentationChunk] = []
    section_occurrences: dict[str, int] = {}
    for section, lines in sections:
        occurrence = section_occurrences.get(section, 0)
        section_occurrences[section] = occurrence + 1
        identity_section = section if occurrence == 0 else f"{section}\0{occurrence}"
        text = "\n".join(lines).strip()
        pieces = _split_large_section(text, max_words)
        for index, piece in enumerate(pieces):
            identity = f"{page.source_path}\0{identity_section}\0{index}"
            chunks.append(
                DocumentationChunk(
                    chunk_id=hashlib.sha256(identity.encode()).hexdigest(),
                    title=page.title,
                    section=section,
                    text=piece,
                    source_path=page.source_path,
                    source_url=page.source_url,
                )
            )
    return chunks


def _split_large_section(text: str, max_words: int) -> list[str]:
    if len(text.split()) <= max_words:
        return [text]
    paragraphs = re.split(r"\n{2,}", text)
    chunks: list[str] = []
    current: list[str] = []
    count = 0
    for paragraph in paragraphs:
        words = paragraph.split()
        if current and count + len(words) > max_words:
            chunks.append("\n\n".join(current))
            current, count = [], 0
        if len(words) > max_words:
            for start in range(0, len(words), max_words):
                if current:
                    chunks.append("\n\n".join(current))
                    current, count = [], 0
                chunks.append(" ".join(words[start:start + max_words]))
        else:
            current.append(paragraph)
            count += len(words)
    if current:
        chunks.append("\n\n".join(current))
    return chunks
