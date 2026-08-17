from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from neo4j import Driver
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.ingestion.github_client import GitHubIssue


class GraphSettings(BaseSettings):
    """Neo4j connection settings loaded from environment variables and .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_username: str
    neo4j_password: SecretStr


class GraphLoader:
    """Load normalized GitHub issues into Neo4j."""

    LOAD_ISSUE_QUERY = """
    MERGE (repository:Repository {owner: $owner, name: $repository_name})
    MERGE (issue:Issue {
        repository: $repository_key,
        issue_number: $issue_number
    })
    SET issue.title = $title,
        issue.body = $body,
        issue.state = $state,
        issue.created_at = $created_at,
        issue.closed_at = $closed_at,
        issue.comments_count = $comments_count,
        issue.html_url = $html_url
    MERGE (repository)-[:HAS_ISSUE]->(issue)
    WITH issue
    UNWIND $labels AS label_name
    MERGE (label:Label {name: label_name})
    MERGE (issue)-[:HAS_LABEL]->(label)
    """

    def __init__(self, driver: Driver) -> None:
        self._driver = driver

    def load_jsonl(self, input_path: Path) -> int:
        """Load all non-empty JSONL records and return the issue count."""

        loaded = 0
        with self._driver.session() as session:
            with input_path.open("r", encoding="utf-8") as input_file:
                for line_number, line in enumerate(input_file, start=1):
                    if not line.strip():
                        continue
                    try:
                        issue = GitHubIssue.model_validate_json(line)
                    except ValueError as error:
                        raise ValueError(
                            f"Invalid issue record on line {line_number}"
                        ) from error
                    session.execute_write(self._merge_issue, issue)
                    loaded += 1
        return loaded

    @classmethod
    def _merge_issue(cls, transaction: Any, issue: GitHubIssue) -> None:
        owner, repository_name = cls._repository_from_url(issue.html_url)
        result = transaction.run(
            cls.LOAD_ISSUE_QUERY,
            owner=owner,
            repository_name=repository_name,
            repository_key=f"{owner}/{repository_name}",
            **issue.model_dump(mode="json", exclude={"labels"}),
            labels=issue.labels,
        )
        result.consume()

    @staticmethod
    def _repository_from_url(html_url: str) -> tuple[str, str]:
        path_parts = urlparse(html_url).path.strip("/").split("/")
        if len(path_parts) < 4 or path_parts[2] != "issues":
            raise ValueError(f"Cannot determine repository from issue URL: {html_url}")
        return path_parts[0], path_parts[1]
