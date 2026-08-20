import re
from typing import Any

from neo4j import Driver
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.retrieval.embeddings import IssueEmbedder
from app.retrieval.vector_store import IssueVectorStore


class GraphRetrieverSettings(BaseSettings):
    """Precision thresholds for deterministic GraphRAG reranking."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    retrieval_min_vector_score: float = Field(default=0.55, ge=0.0, le=1.0)
    retrieval_exceptional_vector_score: float = Field(default=0.90, ge=0.0, le=1.0)
    retrieval_min_final_relevance_score: float = Field(default=0.55, ge=0.0, le=1.0)


class GraphRetriever:
    """Combine vector retrieval with bounded Issue graph expansion."""

    MAX_VECTOR_MATCHES = 5
    MIN_CANDIDATE_POOL = 20
    EXPANSION_LIMIT = 3
    GENERIC_COMPONENTS = {
        "api",
        "kubelet",
        "kubernetes",
        "node",
        "pod",
        "pods",
    }
    TERM_STOP_WORDS = {
        "after",
        "and",
        "are",
        "can",
        "error",
        "enter",
        "for",
        "from",
        "into",
        "issue",
        "kubernetes",
        "not",
        "that",
        "the",
        "this",
        "with",
    }

    MATCHED_ISSUES_QUERY = f"""
    CALL db.index.vector.queryNodes(
        '{IssueVectorStore.INDEX_NAME}', $vector_limit, $embedding
    )
    YIELD node, score
    WITH node AS issue, score
    CALL (issue) {{
        OPTIONAL MATCH (issue)-[:AFFECTS_COMPONENT]->(component:Component)
        RETURN collect(DISTINCT component.name) AS components
    }}
    CALL (issue) {{
        OPTIONAL MATCH (issue)-[:REPORTED_IN_VERSION]->(version:KubernetesVersion)
        RETURN collect(DISTINCT version.version) AS kubernetes_versions
    }}
    CALL (issue) {{
        OPTIONAL MATCH (issue)-[:HAS_LABEL]->(label:Label)
        RETURN collect(DISTINCT label.name) AS labels
    }}
    CALL (issue) {{
        OPTIONAL MATCH (issue)-[:HAS_ERROR]->(error:ErrorMessage)
        RETURN collect(DISTINCT error.message) AS error_messages
    }}
    CALL (issue) {{
        OPTIONAL MATCH (issue)-[:HAS_RESOLUTION]->(resolution:Resolution)
        OPTIONAL MATCH (resolution)-[:SUPPORTED_BY]->(source)
        WITH resolution, collect(DISTINCT source.url) AS evidence_urls
        WITH resolution, evidence_urls,
             CASE WHEN resolution IS NULL THEN null ELSE resolution {{
            resolution_found: true,
            .disposition_verified,
            .actionable_fix_available,
            .resolution_type,
            .root_cause,
            .resolution_summary,
            .verified_steps,
            .confidence,
            evidence_urls: evidence_urls
        }} END AS verified_disposition
        RETURN verified_disposition,
               CASE WHEN resolution.actionable_fix_available = true
                          AND resolution.resolution_type IN ['fixed', 'configuration']
                          AND size(coalesce(resolution.verified_steps, [])) > 0
                    THEN resolution {{
                        .resolution_type,
                        .resolution_summary,
                        .verified_steps,
                        .confidence,
                        evidence_urls: evidence_urls
                    }}
                    ELSE null
               END AS actionable_fix
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
           error_messages,
           verified_disposition,
           actionable_fix
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
    CALL (issue) {
        OPTIONAL MATCH (issue)-[:AFFECTS_COMPONENT]->(component:Component)
        RETURN collect(DISTINCT component.name) AS issue_components
    }
    CALL (issue) {
        OPTIONAL MATCH (issue)-[:REPORTED_IN_VERSION]->(version:KubernetesVersion)
        RETURN collect(DISTINCT version.version) AS issue_versions
    }
    WITH issue, issue_components, issue_versions,
         [source IN $expansion_sources WHERE
             size([name IN issue_components WHERE name IN source.components]) >= 2
             OR (
                 size([name IN issue_components WHERE name IN source.components]) >= 1
                 AND size([name IN issue_versions WHERE name IN source.versions]) >= 1
             )
         ] AS matching_sources
    WHERE size(matching_sources) > 0
    WITH issue,
         [name IN issue_components WHERE name IN $component_names]
             AS shared_components,
         [name IN issue_versions WHERE name IN $version_names]
             AS shared_versions
    WHERE size(shared_components) >= 2
       OR (size(shared_components) >= 1 AND size(shared_versions) >= 1)
    CALL (issue) {
        OPTIONAL MATCH (issue)-[:HAS_RESOLUTION]->(resolution:Resolution)
        OPTIONAL MATCH (resolution)-[:SUPPORTED_BY]->(source)
        WITH resolution, collect(DISTINCT source.url) AS evidence_urls
        WITH resolution, evidence_urls,
             CASE WHEN resolution IS NULL THEN null ELSE resolution {
            resolution_found: true,
            .disposition_verified,
            .actionable_fix_available,
            .resolution_type,
            .root_cause,
            .resolution_summary,
            .verified_steps,
            .confidence,
            evidence_urls: evidence_urls
        } END AS verified_disposition
        RETURN verified_disposition,
               CASE WHEN resolution.actionable_fix_available = true
                          AND resolution.resolution_type IN ['fixed', 'configuration']
                          AND size(coalesce(resolution.verified_steps, [])) > 0
                    THEN resolution {
                        .resolution_type,
                        .resolution_summary,
                        .verified_steps,
                        .confidence,
                        evidence_urls: evidence_urls
                    }
                    ELSE null
               END AS actionable_fix
    }
    RETURN issue.repository AS repository,
           issue.issue_number AS issue_number,
           issue.title AS title,
           issue.summary AS summary,
           issue.html_url AS url,
           shared_components,
           shared_versions,
           verified_disposition,
           actionable_fix
    ORDER BY size(shared_components) + size(shared_versions) DESC,
             issue.issue_number DESC
    LIMIT $expansion_limit
    """

    def __init__(
        self,
        driver: Driver,
        embedder: IssueEmbedder,
        settings: GraphRetrieverSettings | None = None,
    ) -> None:
        self._driver = driver
        self._embedder = embedder
        self._settings = settings or GraphRetrieverSettings()

    def retrieve(
        self,
        query: str,
        limit: int = MAX_VECTOR_MATCHES,
        *,
        components: list[str] | None = None,
        kubernetes_version: str | None = None,
        error_messages: list[str] | None = None,
        symptom: str | None = None,
        debug: bool = False,
    ) -> dict[str, Any]:
        """Rerank vector candidates and expand only accepted direct matches."""

        accepted_limit = min(limit, self.MAX_VECTOR_MATCHES)
        vector_limit = max(self.MIN_CANDIDATE_POOL, accepted_limit * 4)
        embedding = self._embedder.encode_query(query)
        with self._driver.session() as session:
            candidates = [
                dict(record)
                for record in session.run(
                    self.MATCHED_ISSUES_QUERY,
                    embedding=embedding,
                    vector_limit=vector_limit,
                ).data()
            ]
            accepted: list[dict[str, Any]] = []
            rejected: list[dict[str, Any]] = []
            for candidate in candidates:
                explanation = self._relevance_explanation(
                    candidate,
                    query=query,
                    components=components or [],
                    kubernetes_version=kubernetes_version,
                    error_messages=error_messages or [],
                    symptom=symptom,
                )
                candidate["relevance"] = explanation
                if explanation["accepted"]:
                    accepted.append(candidate)
                else:
                    rejected.append(candidate)
            accepted.sort(
                key=lambda item: (
                    item["relevance"]["final_relevance_score"],
                    item["relevance"]["vector_similarity"],
                ),
                reverse=True,
            )
            matched = accepted[:accepted_limit]

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
                        expansion_sources=[
                            {
                                "components": issue.get("components", []),
                                "versions": issue.get("kubernetes_versions", []),
                            }
                            for issue in matched
                        ],
                        expansion_limit=self.EXPANSION_LIMIT,
                    ).data()
                ]

        return {
            "query": query,
            "matched_issues": matched,
            "related_issues": related,
            **(
                {
                    "debug": {
                        "candidate_pool_size": len(candidates),
                        "accepted_count": len(matched),
                        "rejected_candidates": [
                            {
                                "issue_number": issue.get("issue_number"),
                                "title": issue.get("title"),
                                "relevance": issue["relevance"],
                            }
                            for issue in rejected
                        ],
                    }
                }
                if debug
                else {}
            ),
        }

    def _relevance_explanation(
        self,
        candidate: dict[str, Any],
        *,
        query: str,
        components: list[str],
        kubernetes_version: str | None,
        error_messages: list[str],
        symptom: str | None,
    ) -> dict[str, Any]:
        vector_similarity = float(candidate.get("score") or 0.0)
        requested_components = self._normalized_values(components)
        candidate_components = self._normalized_values(
            candidate.get("components", [])
        )
        matching_components = sorted(requested_components & candidate_components)
        requested_versions = self._normalized_values(
            [kubernetes_version] if kubernetes_version else []
        )
        candidate_versions = self._normalized_values(
            candidate.get("kubernetes_versions", [])
        )
        matching_versions = sorted(requested_versions & candidate_versions)
        diagnostic_terms = self._distinctive_terms(
            " ".join([query, symptom or "", *error_messages])
        )
        diagnostic_terms -= requested_components | self.GENERIC_COMPONENTS
        candidate_terms = self._distinctive_terms(
            " ".join(
                [
                    str(candidate.get("title") or ""),
                    str(candidate.get("summary") or ""),
                    *candidate.get("error_messages", []),
                ]
            )
        )
        matching_error_terms = sorted(diagnostic_terms & candidate_terms)
        meaningful_components = [
            component
            for component in matching_components
            if component not in self.GENERIC_COMPONENTS
        ]
        has_meaningful_signal = bool(
            meaningful_components or matching_versions or matching_error_terms
        )
        final_score = min(
            1.0,
            vector_similarity * 0.70
            + (0.10 if meaningful_components else 0.0)
            + (0.10 if matching_versions else 0.0)
            + (0.20 if matching_error_terms else 0.0),
        )
        exceptional = (
            vector_similarity >= self._settings.retrieval_exceptional_vector_score
        )
        accepted = exceptional or (
            vector_similarity >= self._settings.retrieval_min_vector_score
            and has_meaningful_signal
            and final_score >= self._settings.retrieval_min_final_relevance_score
        )
        return {
            "vector_similarity": vector_similarity,
            "matching_components": matching_components,
            "matching_versions": matching_versions,
            "matching_error_terms": matching_error_terms,
            "final_relevance_score": round(final_score, 6),
            "accepted": accepted,
            "accepted_by": (
                "exceptional_vector"
                if exceptional
                else "vector_plus_diagnostic_overlap"
                if accepted
                else None
            ),
        }

    @staticmethod
    def _normalized_values(values: list[str]) -> set[str]:
        return {value.strip().lower() for value in values if value and value.strip()}

    @classmethod
    def _distinctive_terms(cls, value: str) -> set[str]:
        return {
            term
            for term in re.findall(r"[a-z0-9]+", value.lower())
            if len(term) >= 4 and term not in cls.TERM_STOP_WORDS
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
