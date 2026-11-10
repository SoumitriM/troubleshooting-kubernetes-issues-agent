"""Separate SQLite vector store for official documentation chunks."""

import hashlib
import json
import re
import sqlite3
import threading
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy

from app.documentation.models import DocumentationChunk
from app.documentation.parser import chunk_page, iter_english_pages
from app.retrieval.embeddings import IssueEmbedder


# Hybrid retrieval: dense embeddings miss exact section vocabulary such as
# "desiredReplicas" or a page's own subject, so a BM25 ranking over title,
# section and text is fused with the vector ranking (reciprocal rank fusion).
CANDIDATES_PER_RANKING = 50
RRF_K = 60
BM25_K1 = 1.2
BM25_B = 0.75
MAX_CHUNKS_PER_PAGE = 3
REFERENCE_PAGE_MARKERS = ("/reference/kubernetes-api/", "/feature-gates/")
REFERENCE_PAGE_WEIGHT = 0.75
STOPWORDS = frozenset(
    "a an and are as at be by can do does for from how i in is it its of on or "
    "that the this to what when where which who why will with you your".split()
)


def _stem(token: str) -> str:
    for suffix in ("ations", "ation", "ings", "ing", "ers", "er", "ies", "es", "s", "ed"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return token


def lexical_tokens(text: str) -> list[str]:
    """Lowercase stemmed tokens; CamelCase API names also yield their parts."""

    tokens: list[str] = []
    for word in re.findall(r"[A-Za-z0-9]+", text):
        parts = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+", word)
        for token in {word.lower(), *(part.lower() for part in parts)}:
            if len(token) > 1 and token not in STOPWORDS:
                tokens.append(_stem(token))
    return tokens


class DocumentationStore:
    def __init__(self, path: Path, embedder: IssueEmbedder) -> None:
        self._path = path
        self._embedder = embedder
        self._cache_lock = threading.Lock()
        self._cache_key: tuple[int, int] | None = None
        self._cache_rows: list[tuple[str, str, str, str, str]] = []
        self._cache_matrix = numpy.zeros((0, 0), dtype=numpy.float32)
        self._cache_postings: dict[str, tuple[numpy.ndarray, numpy.ndarray]] = {}
        self._cache_lengths = numpy.zeros(0, dtype=numpy.float32)
        self._cache_title_terms: list[frozenset[str]] = []

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

    def preload(self) -> None:
        """Decode the index ahead of the first query when it already exists."""

        if self._path.exists():
            self._load_index()

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Return the best chunks by fused vector and BM25 rank.

        At most MAX_CHUNKS_PER_PAGE chunks come from one page, so a single long
        page cannot crowd out others while the answer still sees several
        sections of the most relevant page.
        """

        if not self._path.exists():
            return []
        rows, matrix = self._load_index()
        if not rows:
            return []
        query_vector = numpy.asarray(
            self._embedder.encode_query(query), dtype=numpy.float32
        )
        query_norm = numpy.linalg.norm(query_vector)
        if query_norm:
            query_vector = query_vector / query_norm
        vector_scores = matrix @ query_vector
        lexical_scores = self._bm25(query)
        # Favour chunks of pages whose title names the question's subject, so
        # "What is a PersistentVolumeClaim?" reaches the Persistent Volumes page.
        query_terms = set(lexical_tokens(query))
        title_scores = numpy.asarray(
            [
                len(terms & query_terms) / len(terms) if terms else 0.0
                for terms in self._cache_title_terms
            ],
            dtype=numpy.float32,
        )
        title_scores = numpy.where(title_scores >= 0.5, title_scores + vector_scores, 0.0)
        fused: dict[int, float] = defaultdict(float)
        # Vector ranks always count; lexical and title ranks only where they matched.
        for scores, sparse in (
            (vector_scores, False), (lexical_scores, True), (title_scores, True)
        ):
            ranked = numpy.argsort(-scores, kind="stable")[:CANDIDATES_PER_RANKING]
            for rank, index in enumerate(ranked):
                if not sparse or scores[index] > 0:
                    fused[int(index)] += 1.0 / (RRF_K + rank + 1)
        # Generated API and feature-gate reference pages match names strongly
        # but rarely explain behaviour; prefer concept, task and tutorial pages.
        for index in fused:
            if any(part in rows[index][4] for part in REFERENCE_PAGE_MARKERS):
                fused[index] *= REFERENCE_PAGE_WEIGHT
        count = max(1, min(limit, 20))
        per_page: Counter[str] = Counter()
        selected: list[int] = []
        for index in sorted(fused, key=lambda item: (-fused[item], item)):
            url = rows[index][4]
            if per_page[url] >= MAX_CHUNKS_PER_PAGE:
                continue
            per_page[url] += 1
            selected.append(index)
            if len(selected) == count:
                break
        return [
            {
                "title": rows[index][0],
                "section": rows[index][1],
                "text": rows[index][2],
                "source_path": rows[index][3],
                "source_url": rows[index][4],
                "score": float(vector_scores[index]),
            }
            for index in selected
        ]

    def _bm25(self, query: str) -> numpy.ndarray:
        lengths = self._cache_lengths
        scores = numpy.zeros(len(lengths), dtype=numpy.float32)
        if not len(lengths):
            return scores
        average = float(lengths.mean()) or 1.0
        for token in set(lexical_tokens(query)):
            posting = self._cache_postings.get(token)
            if posting is None:
                continue
            rows, frequencies = posting
            idf = numpy.log(1 + (len(lengths) - len(rows) + 0.5) / (len(rows) + 0.5))
            norm = BM25_K1 * (1 - BM25_B + BM25_B * lengths[rows] / average)
            scores[rows] += idf * frequencies * (BM25_K1 + 1) / (frequencies + norm)
        return scores

    def _load_index(
        self,
    ) -> tuple[list[tuple[str, str, str, str, str]], numpy.ndarray]:
        """Decode embeddings once and reload only after the index file changes."""

        stat = self._path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        with self._cache_lock:
            if key != self._cache_key:
                with self._connect() as connection:
                    self._create_schema(connection)
                    records = connection.execute(
                        """
                        SELECT title, section, text, source_path, source_url,
                               embedding
                        FROM documentation_chunks
                        ORDER BY chunk_id
                        """
                    ).fetchall()
                matrix = numpy.asarray(
                    [json.loads(record[5]) for record in records],
                    dtype=numpy.float32,
                )
                if records:
                    norms = numpy.linalg.norm(matrix, axis=1, keepdims=True)
                    matrix = numpy.divide(
                        matrix, norms, out=numpy.zeros_like(matrix), where=norms > 0
                    )
                self._cache_rows = [tuple(record[:5]) for record in records]
                self._cache_matrix = matrix
                self._build_lexical_index(self._cache_rows)
                self._cache_key = key
            return self._cache_rows, self._cache_matrix

    def _build_lexical_index(self, rows: list[tuple[str, str, str, str, str]]) -> None:
        postings: dict[str, tuple[list[int], list[int]]] = defaultdict(lambda: ([], []))
        lengths = []
        for index, (title, section, text, _path, _url) in enumerate(rows):
            tokens = lexical_tokens(f"{title} {section} {text}")
            lengths.append(len(tokens))
            for token, frequency in Counter(tokens).items():
                indexes, frequencies = postings[token]
                indexes.append(index)
                frequencies.append(frequency)
        self._cache_postings = {
            token: (
                numpy.asarray(indexes, dtype=numpy.int64),
                numpy.asarray(frequencies, dtype=numpy.float32),
            )
            for token, (indexes, frequencies) in postings.items()
        }
        self._cache_lengths = numpy.asarray(lengths, dtype=numpy.float32)
        titles: dict[str, frozenset[str]] = {}
        self._cache_title_terms = [
            titles.setdefault(title, frozenset(lexical_tokens(title)))
            for title, *_rest in rows
        ]

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


class KubernetesDocsRetriever:
    def __init__(self, store: DocumentationStore) -> None:
        self._store = store

    def preload(self) -> None:
        """Decode the index ahead of the first query when it already exists."""

        if self._path.exists():
            self._load_index()

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return self._store.search(query, limit)
