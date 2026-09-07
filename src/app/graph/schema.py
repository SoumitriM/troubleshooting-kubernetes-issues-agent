"""Application-owned description of the Neo4j schema used by agents."""

NODE_LABELS = frozenset(
    {
        "Repository",
        "Issue",
        "Label",
        "Component",
        "KubernetesVersion",
        "ErrorMessage",
        "Resolution",
        "PullRequest",
        "Commit",
        "Comment",
        "EvidenceSource",
    }
)

RELATIONSHIP_TYPES = frozenset(
    {
        "HAS_ISSUE",
        "HAS_LABEL",
        "AFFECTS_COMPONENT",
        "REPORTED_IN_VERSION",
        "HAS_ERROR",
        "HAS_RESOLUTION",
        "LINKED_TO_PR",
        "CLOSED_BY",
        "HAS_COMMENT",
        "SUPPORTED_BY",
    }
)

SCHEMA_DESCRIPTION = """
Nodes:
- Repository(owner, name)
- Issue(repository, issue_number, title, body, state, created_at, closed_at,
  comments_count, html_url, summary, symptom, expected_behavior)
- Label(name), Component(name), KubernetesVersion(version), ErrorMessage(message)
- Resolution(repository, issue_number, resolution_type, disposition_verified,
  actionable_fix_available, root_cause, resolution_summary, verified_steps,
  confidence)
- PullRequest(url, number, title, state), Commit(sha, url)
- Comment(id, author, body, created_at, url), EvidenceSource(url)
Relationships:
- (Repository)-[:HAS_ISSUE]->(Issue)
- (Issue)-[:HAS_LABEL]->(Label)
- (Issue)-[:AFFECTS_COMPONENT]->(Component)
- (Issue)-[:REPORTED_IN_VERSION]->(KubernetesVersion)
- (Issue)-[:HAS_ERROR]->(ErrorMessage)
- (Issue)-[:HAS_RESOLUTION]->(Resolution)
- (Issue)-[:LINKED_TO_PR]->(PullRequest)
- (Issue)-[:CLOSED_BY]->(Commit)
- (Issue)-[:HAS_COMMENT]->(Comment)
- (Resolution)-[:SUPPORTED_BY]->(PullRequest|Commit|Comment|EvidenceSource)
""".strip()
