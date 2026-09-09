"""Separate SQLite vector store for official documentation chunks."""

import hashlib
import json
import math
import sqlite3
from pathlib import Path
from typing import Any

from app.documentation.models import DocumentationChunk
from app.documentation.parser import chunk_page, iter_english_pages
from app.retrieval.embeddings import IssueEmbedder


class DocumentationStore:
    def __init__(self, path: Path, embedder: IssueEmbedder) -> None:
        self._path = path
        self._embedder = embedder

    def index_repository(self, repository_root: Path) -> dict[str, int]:
        chunks = [
            chunk
            for page in iter_english_pages(repository_root)
            for chunk in chunk_page(page)
        ]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            self._create_schema(connection)
            existing = {
                row[0]: (row[1], row[2])
                for row in connection.execute(
                    "SELECT chunk_id, text_hash, embedding FROM documentation_chunks"
                )
            }
            changed = [
                chunk
                for chunk in chunks
                if existing.get(chunk.chunk_id, (None, None))[0]
                != self._text_hash(chunk.text)
            ]
            vectors = self._embedder.encode([chunk.text for chunk in changed])
            changed_vectors = dict(
                zip((chunk.chunk_id for chunk in changed), vectors, strict=True)
            )
            for chunk in chunks:
                text_hash = self._text_hash(chunk.text)
                vector = changed_vectors.get(chunk.chunk_id)
                embedding = (
                    json.dumps(vector)
                    if vector is not None
                    else existing[chunk.chunk_id][1]
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO documentation_chunks
                    (chunk_id, title, section, text, source_path, source_url,
                     text_hash, embedding)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.chunk_id,
                        chunk.title,
                        chunk.section,
                        chunk.text,
                        chunk.source_path,
                        chunk.source_url,
                        text_hash,
                        embedding,
                    ),
                )
            current_ids = {chunk.chunk_id for chunk in chunks}
            stale = set(existing) - current_ids
            connection.executemany(
                "DELETE FROM documentation_chunks WHERE chunk_id = ?",
                ((chunk_id,) for chunk_id in stale),
            )
        return {"total_chunks": len(chunks), "changed_chunks": len(changed)}

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        query_vector = self._embedder.encode_query(query)
        with self._connect() as connection:
            self._create_schema(connection)
            rows = connection.execute(
                """
                SELECT title, section, text, source_path, source_url, embedding
                FROM documentation_chunks
                """
            ).fetchall()
        scored = [
            {
                "title": row[0],
                "section": row[1],
                "text": row[2],
                "source_path": row[3],
                "source_url": row[4],
                "score": self._cosine(query_vector, json.loads(row[5])),
            }
            for row in rows
        ]
        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored[: max(1, min(limit, 20))]

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS documentation_chunks (
                chunk_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                section TEXT NOT NULL,
                text TEXT NOT NULL,
                source_path TEXT NOT NULL,
                source_url TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                embedding TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _text_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        numerator = sum(a * b for a, b in zip(left, right, strict=True))
        denominator = math.sqrt(sum(a * a for a in left)) * math.sqrt(
            sum(b * b for b in right)
        )
        return numerator / denominator if denominator else 0.0


class KubernetesDocsRetriever:
    def __init__(self, store: DocumentationStore) -> None:
        self._store = store

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return self._store.search(query, limit)
