import hashlib
from dataclasses import dataclass
from typing import Any

from neo4j import Driver

from app.retrieval.embeddings import EMBEDDING_DIMENSIONS, IssueEmbedder


@dataclass(frozen=True)
class IssueDocument:
    repository: str
    issue_number: int
    text: str
    text_hash: str


class IssueVectorStore:
    """Index and search Issue nodes using Neo4j's vector index."""

    INDEX_NAME = "issue_embedding_index"
    CREATE_INDEX_QUERY = f"""
    CREATE VECTOR INDEX {INDEX_NAME} IF NOT EXISTS
    FOR (issue:Issue) ON (issue.embedding)
    OPTIONS {{indexConfig: {{
        `vector.dimensions`: {EMBEDDING_DIMENSIONS},
        `vector.similarity_function`: 'cosine'
    }}}}
    """
    LIST_ISSUES_QUERY = """
    MATCH (issue:Issue)
    RETURN issue.repository AS repository,
           issue.issue_number AS issue_number,
           issue.title AS title,
           issue.summary AS summary,
           issue.symptom AS symptom,
           issue.expected_behavior AS expected_behavior,
           issue.embedding_text_hash AS embedding_text_hash
    ORDER BY repository, issue_number
    """
    UPDATE_EMBEDDING_QUERY = """
    MATCH (issue:Issue {repository: $repository, issue_number: $issue_number})
    SET issue.embedding = $embedding,
        issue.embedding_text_hash = $text_hash
    """
    SEARCH_QUERY = f"""
    CALL db.index.vector.queryNodes('{INDEX_NAME}', $limit, $embedding)
    YIELD node, score
    RETURN node.issue_number AS issue_number,
           node.title AS title,
           node.summary AS summary,
           node.html_url AS url,
           score
    ORDER BY score DESC
    """

    def __init__(self, driver: Driver, embedder: IssueEmbedder) -> None:
        self._driver = driver
        self._embedder = embedder

    def index_issues(self) -> int:
        """Embed only Issue nodes whose searchable content has changed."""

        with self._driver.session() as session:
            session.run(self.CREATE_INDEX_QUERY).consume()
            records = session.run(self.LIST_ISSUES_QUERY).data()
            changed: list[IssueDocument] = []
            for record in records:
                text = self.build_searchable_text(record)
                text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if record.get("embedding_text_hash") == text_hash:
                    continue
                changed.append(
                    IssueDocument(
                        repository=record["repository"],
                        issue_number=record["issue_number"],
                        text=text,
                        text_hash=text_hash,
                    )
                )

            embeddings = self._embedder.encode([issue.text for issue in changed])
            for issue, embedding in zip(changed, embeddings, strict=True):
                session.run(
                    self.UPDATE_EMBEDDING_QUERY,
                    repository=issue.repository,
                    issue_number=issue.issue_number,
                    embedding=embedding,
                    text_hash=issue.text_hash,
                ).consume()
        return len(changed)

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Return the nearest Issue nodes for a natural-language query."""

        embedding = self._embedder.encode_query(query)
        with self._driver.session() as session:
            records = session.run(
                self.SEARCH_QUERY,
                embedding=embedding,
                limit=limit,
            ).data()
        return [dict(record) for record in records]

    @staticmethod
    def build_searchable_text(issue: dict[str, Any]) -> str:
        fields = (
            ("Title", issue.get("title")),
            ("Summary", issue.get("summary")),
            ("Symptom", issue.get("symptom")),
            ("Expected behavior", issue.get("expected_behavior")),
        )
        return "\n".join(
            f"{label}: {str(value).strip()}"
            for label, value in fields
            if value is not None and str(value).strip()
        )
