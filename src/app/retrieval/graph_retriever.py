from typing import Any

from neo4j import Driver

from app.retrieval.embeddings import IssueEmbedder
from app.retrieval.vector_store import IssueVectorStore


class GraphRetriever:
    """Combine vector retrieval with bounded Issue graph expansion."""

    MAX_VECTOR_MATCHES = 5
    EXPANSION_LIMIT = 3

    MATCHED_ISSUES_QUERY = f"""
    CALL db.index.vector.queryNodes(
        '{IssueVectorStore.INDEX_NAME}', $vector_limit, $embedding
    )
    YIELD node, score
    WITH node AS issue, score
    CALL {{
        WITH issue
        OPTIONAL MATCH (issue)-[:AFFECTS_COMPONENT]->(component:Component)
        RETURN collect(DISTINCT component.name) AS components
    }}
    CALL {{
        WITH issue
        OPTIONAL MATCH (issue)-[:REPORTED_IN_VERSION]->(version:KubernetesVersion)
        RETURN collect(DISTINCT version.version) AS kubernetes_versions
    }}
    CALL {{
        WITH issue
        OPTIONAL MATCH (issue)-[:HAS_LABEL]->(label:Label)
        RETURN collect(DISTINCT label.name) AS labels
    }}
    CALL {{
        WITH issue
        OPTIONAL MATCH (issue)-[:HAS_ERROR]->(error:ErrorMessage)
        RETURN collect(DISTINCT error.message) AS error_messages
    }}
    RETURN issue.repository AS repository,
           issue.issue_number AS issue_number,
           issue.title AS title,
           issue.summary AS summary,
           issue.html_url AS url,
           score,
           components,
           kubernetes_versions,
           labels,
           error_messages
    ORDER BY score DESC
    """

    RELATED_ISSUES_QUERY = """
    MATCH (issue:Issue)
    WHERE NOT (
        {repository: issue.repository, issue_number: issue.issue_number}
        IN $matched_issue_keys
    )
      AND (
          EXISTS {
              MATCH (issue)-[:AFFECTS_COMPONENT]->(component:Component)
              WHERE component.name IN $component_names
          }
          OR EXISTS {
              MATCH (issue)-[:REPORTED_IN_VERSION]->(version:KubernetesVersion)
              WHERE version.version IN $version_names
          }
      )
    CALL {
        WITH issue
        OPTIONAL MATCH (issue)-[:AFFECTS_COMPONENT]->(component:Component)
        RETURN collect(DISTINCT component.name) AS issue_components
    }
    CALL {
        WITH issue
        OPTIONAL MATCH (issue)-[:REPORTED_IN_VERSION]->(version:KubernetesVersion)
        RETURN collect(DISTINCT version.version) AS issue_versions
    }
    WITH issue,
         [name IN issue_components WHERE name IN $component_names]
             AS shared_components,
         [name IN issue_versions WHERE name IN $version_names]
             AS shared_versions
    WHERE size(shared_components) > 0 OR size(shared_versions) > 0
    RETURN issue.repository AS repository,
           issue.issue_number AS issue_number,
           issue.title AS title,
           issue.summary AS summary,
           issue.html_url AS url,
           shared_components,
           shared_versions
    ORDER BY size(shared_components) + size(shared_versions) DESC,
             issue.issue_number DESC
    LIMIT $expansion_limit
    """

    def __init__(self, driver: Driver, embedder: IssueEmbedder) -> None:
        self._driver = driver
        self._embedder = embedder

    def retrieve(self, query: str, limit: int = MAX_VECTOR_MATCHES) -> dict[str, Any]:
        """Return vector matches and a bounded set of graph-related issues."""

        vector_limit = min(limit, self.MAX_VECTOR_MATCHES)
        embedding = self._embedder.encode_query(query)
        with self._driver.session() as session:
            matched = [
                dict(record)
                for record in session.run(
                    self.MATCHED_ISSUES_QUERY,
                    embedding=embedding,
                    vector_limit=vector_limit,
                ).data()
            ]

            matched_keys = [
                {
                    "repository": issue["repository"],
                    "issue_number": issue["issue_number"],
                }
                for issue in matched
            ]
            component_names = self._unique_values(matched, "components")
            version_names = self._unique_values(matched, "kubernetes_versions")
            related = []
            if matched and (component_names or version_names):
                related = [
                    dict(record)
                    for record in session.run(
                        self.RELATED_ISSUES_QUERY,
                        matched_issue_keys=matched_keys,
                        component_names=component_names,
                        version_names=version_names,
                        expansion_limit=self.EXPANSION_LIMIT,
                    ).data()
                ]

        return {
            "query": query,
            "matched_issues": matched,
            "related_issues": related,
        }

    @staticmethod
    def _unique_values(records: list[dict[str, Any]], field: str) -> list[str]:
        return list(
            dict.fromkeys(
                value
                for record in records
                for value in record.get(field, [])
                if value
            )
        )
