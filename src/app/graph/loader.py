from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from neo4j import Driver
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.enrichment.models import IssueEnrichment
from app.enrichment.resolution_models import IssueResolution
from app.ingestion.github_client import GitHubIssue
from app.ingestion.resolution_client import ResolutionEvidence


class GraphSettings(BaseSettings):
    """Neo4j connection settings loaded from environment variables and .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_username: str
    neo4j_password: SecretStr


class GraphIssueRecord(GitHubIssue):
    """A normalized issue with optional FH Genie enrichment."""

    enrichment: IssueEnrichment | None = None
    resolution_evidence: ResolutionEvidence | None = None
    resolution: IssueResolution | None = None


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
    FOREACH (_ IN CASE WHEN $has_enrichment THEN [1] ELSE [] END |
        SET issue.summary = $summary,
            issue.symptom = $symptom,
            issue.expected_behavior = $expected_behavior
    )
    FOREACH (label_name IN $labels |
        MERGE (label:Label {name: label_name})
        MERGE (issue)-[:HAS_LABEL]->(label)
    )
    FOREACH (component_name IN $components |
        MERGE (component:Component {name: component_name})
        MERGE (issue)-[:AFFECTS_COMPONENT]->(component)
    )
    FOREACH (version_name IN $kubernetes_versions |
        MERGE (version:KubernetesVersion {version: version_name})
        MERGE (issue)-[:REPORTED_IN_VERSION]->(version)
    )
    FOREACH (error_text IN $error_messages |
        MERGE (error:ErrorMessage {message: error_text})
        MERGE (issue)-[:HAS_ERROR]->(error)
    )
    FOREACH (_ IN CASE WHEN $has_resolution THEN [1] ELSE [] END |
        MERGE (resolution:Resolution {
            repository: $repository_key,
            issue_number: $issue_number
        })
        SET resolution.resolution_type = $resolution_type,
            resolution.disposition_verified = $disposition_verified,
            resolution.actionable_fix_available = $actionable_fix_available,
            resolution.root_cause = $root_cause,
            resolution.resolution_summary = $resolution_summary,
            resolution.verified_steps = $verified_steps,
            resolution.confidence = $confidence
        MERGE (issue)-[:HAS_RESOLUTION]->(resolution)
    )
    FOREACH (pull IN $pull_requests |
        MERGE (pull_request:PullRequest {url: pull.url})
        SET pull_request.number = pull.number,
            pull_request.title = pull.title,
            pull_request.state = pull.state
        MERGE (issue)-[:LINKED_TO_PR]->(pull_request)
    )
    FOREACH (pull_url IN $supported_pull_request_urls |
        MERGE (resolution:Resolution {
            repository: $repository_key,
            issue_number: $issue_number
        })
        MERGE (pull_request:PullRequest {url: pull_url})
        MERGE (resolution)-[:SUPPORTED_BY]->(pull_request)
    )
    FOREACH (commit_data IN $closing_commits |
        MERGE (commit:Commit {sha: commit_data.sha})
        SET commit.url = commit_data.url
        MERGE (issue)-[:CLOSED_BY]->(commit)
    )
    FOREACH (sha IN $supported_commit_shas |
        MERGE (resolution:Resolution {
            repository: $repository_key,
            issue_number: $issue_number
        })
        MERGE (commit:Commit {sha: sha})
        MERGE (resolution)-[:SUPPORTED_BY]->(commit)
    )
    FOREACH (comment_data IN $evidence_comments |
        MERGE (comment:Comment {id: comment_data.id})
        SET comment.author = comment_data.author,
            comment.body = comment_data.body,
            comment.created_at = comment_data.created_at,
            comment.url = comment_data.url
        MERGE (issue)-[:HAS_COMMENT]->(comment)
        MERGE (resolution:Resolution {
            repository: $repository_key,
            issue_number: $issue_number
        })
        MERGE (resolution)-[:SUPPORTED_BY]->(comment)
    )
    FOREACH (evidence_url IN $unmatched_evidence_urls |
        MERGE (source:EvidenceSource {url: evidence_url})
        MERGE (resolution:Resolution {
            repository: $repository_key,
            issue_number: $issue_number
        })
        MERGE (resolution)-[:SUPPORTED_BY]->(source)
    )
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
                        issue = GraphIssueRecord.model_validate_json(line)
                    except ValueError as error:
                        raise ValueError(
                            f"Invalid issue record on line {line_number}"
                        ) from error
                    session.execute_write(self._merge_issue, issue)
                    loaded += 1
        return loaded

    @classmethod
    def _merge_issue(cls, transaction: Any, issue: GraphIssueRecord) -> None:
        owner, repository_name = cls._repository_from_url(issue.html_url)
        enrichment = issue.enrichment
        resolution = issue.resolution
        has_resolution = resolution is not None and resolution.resolution_found
        evidence = issue.resolution_evidence
        evidence_urls = set(resolution.evidence_urls) if has_resolution else set()
        pull_requests = evidence.linked_pull_requests if evidence and has_resolution else []
        closing_commits = evidence.closing_commits if evidence and has_resolution else []
        evidence_comments = (
            [comment for comment in evidence.comments if comment.url in evidence_urls]
            if evidence and has_resolution
            else []
        )
        matched_urls = {
            comment.url for comment in evidence_comments
        } | {
            pull.url for pull in pull_requests if pull.url in evidence_urls
        } | {
            commit.url for commit in closing_commits if commit.url in evidence_urls
        }
        result = transaction.run(
            cls.LOAD_ISSUE_QUERY,
            owner=owner,
            repository_name=repository_name,
            repository_key=f"{owner}/{repository_name}",
            **issue.model_dump(
                mode="json",
                exclude={
                    "labels",
                    "enrichment",
                    "resolution_evidence",
                    "resolution",
                },
            ),
            labels=issue.labels,
            has_enrichment=enrichment is not None,
            summary=enrichment.summary if enrichment else None,
            symptom=enrichment.symptom if enrichment else None,
            expected_behavior=(
                enrichment.expected_behavior if enrichment else None
            ),
            components=(
                cls._normalized_components(enrichment.components)
                if enrichment
                else []
            ),
            kubernetes_versions=(
                cls._non_empty_unique(enrichment.kubernetes_versions)
                if enrichment
                else []
            ),
            error_messages=(
                cls._non_empty_unique(enrichment.error_messages)
                if enrichment
                else []
            ),
            has_resolution=has_resolution,
            resolution_type=resolution.resolution_type if has_resolution else None,
            disposition_verified=(
                resolution.disposition_verified if has_resolution else False
            ),
            actionable_fix_available=(
                resolution.actionable_fix_available if has_resolution else False
            ),
            root_cause=resolution.root_cause if has_resolution else None,
            resolution_summary=(
                resolution.resolution_summary if has_resolution else None
            ),
            verified_steps=resolution.verified_steps if has_resolution else [],
            confidence=resolution.confidence if has_resolution else None,
            pull_requests=[pull.model_dump(mode="json") for pull in pull_requests],
            supported_pull_request_urls=[
                pull.url for pull in pull_requests if pull.url in evidence_urls
            ],
            closing_commits=[
                commit.model_dump(mode="json") for commit in closing_commits
            ],
            supported_commit_shas=[
                commit.sha for commit in closing_commits if commit.url in evidence_urls
            ],
            evidence_comments=[
                comment.model_dump(mode="json") for comment in evidence_comments
            ],
            unmatched_evidence_urls=list(evidence_urls - matched_urls),
        )
        result.consume()

    @staticmethod
    def _normalized_components(components: list[str]) -> list[str]:
        """Normalize component identity while retaining first-seen order."""

        normalized = (component.strip().lower() for component in components)
        return list(dict.fromkeys(component for component in normalized if component))

    @staticmethod
    def _non_empty_unique(values: list[str]) -> list[str]:
        """Remove blank and duplicate values while retaining source text."""

        return list(dict.fromkeys(value for value in values if value.strip()))

    @staticmethod
    def _repository_from_url(html_url: str) -> tuple[str, str]:
        path_parts = urlparse(html_url).path.strip("/").split("/")
        if len(path_parts) < 4 or path_parts[2] != "issues":
            raise ValueError(f"Cannot determine repository from issue URL: {html_url}")
        return path_parts[0], path_parts[1]
