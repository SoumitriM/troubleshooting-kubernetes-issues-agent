import re
from typing import Any

import snowballstemmer
from neo4j import Driver
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agent.state import QueryAnalysis
from app.retrieval.embeddings import IssueEmbedder
from app.retrieval.vector_store import IssueVectorStore


class GraphRetrieverSettings(BaseSettings):
    """Precision thresholds for deterministic GraphRAG reranking."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    retrieval_min_vector_score: float = Field(default=0.55, ge=0.0, le=1.0)
    retrieval_exceptional_vector_score: float = Field(default=0.90, ge=0.0, le=1.0)
    retrieval_min_final_relevance_score: float = Field(default=0.55, ge=0.0, le=1.0)
    retrieval_min_relative_score_ratio: float = Field(default=0.85, ge=0.0, le=1.0)


class GraphRetriever:
    """Combine vector retrieval with bounded Issue graph expansion."""

    MAX_VECTOR_MATCHES = 5
    MIN_CANDIDATE_POOL = 20
    EXPANSION_LIMIT = 3
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
        "pod",
        "pods",
        "problem",
        "not",
        "that",
        "the",
        "this",
        "with",
    }
    KNOWN_CRITICAL_ERRORS = {
        "crashloopbackoff",
        "imagepullbackoff",
        "oomkilled",
    }
    GENERIC_ERRORS = {"broken", "error", "failed", "failure", "problem"}
    STEMMER = snowballstemmer.stemmer("english")
    GENERIC_SYMPTOM_WORDS = {
        "break",
        "broken",
        "fail",
        "failure",
        "restart",
        "work",
    }
    GENERIC_SYMPTOM_STEMS = frozenset(
        STEMMER.stemWords(GENERIC_SYMPTOM_WORDS)
    )

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

    def search(
        self,
        *,
        query_text: str,
        query_analysis: QueryAnalysis | None = None,
        limit: int = MAX_VECTOR_MATCHES,
        debug: bool = False,
    ) -> dict[str, Any]:
        """Search by raw text and optionally apply structured deterministic reranking."""

        accepted_limit = min(limit, self.MAX_VECTOR_MATCHES)
        vector_limit = max(self.MIN_CANDIDATE_POOL, accepted_limit * 4)
        embedding = self._embedder.encode_query(query_text)
        with self._driver.session() as session:
            candidates = [
                dict(record)
                for record in session.run(
                    self.MATCHED_ISSUES_QUERY,
                    embedding=embedding,
                    vector_limit=vector_limit,
                ).data()
            ]
            provisionally_accepted: list[dict[str, Any]] = []
            rejected: list[dict[str, Any]] = []
            cross_version: list[dict[str, Any]] = []
            for candidate in candidates:
                explanation = (
                    self._relevance_explanation(candidate, query_analysis)
                    if query_analysis is not None
                    else self._raw_relevance_explanation(candidate)
                )
                candidate["relevance"] = explanation
                if explanation["accepted"]:
                    provisionally_accepted.append(candidate)
                elif explanation.get("cross_version_match"):
                    cross_version.append(candidate)
                else:
                    rejected.append(candidate)
            provisionally_accepted.sort(
                key=lambda item: (
                    item["relevance"]["final_relevance_score"],
                    item["relevance"]["vector_similarity"],
                ),
                reverse=True,
            )
            accepted: list[dict[str, Any]] = []
            if provisionally_accepted:
                best_score = provisionally_accepted[0]["relevance"][
                    "final_relevance_score"
                ]
                relative_cutoff = (
                    best_score * self._settings.retrieval_min_relative_score_ratio
                )
                for candidate in provisionally_accepted:
                    if candidate["relevance"]["final_relevance_score"] >= relative_cutoff:
                        accepted.append(candidate)
                    else:
                        candidate["relevance"]["accepted"] = False
                        candidate["relevance"]["accepted_by"] = None
                        candidate["relevance"]["rejection_reasons"].append(
                            "outside_relative_score_window"
                        )
                        rejected.append(candidate)
            matched = accepted[:accepted_limit]
            cross_version.sort(
                key=lambda item: (
                    item["relevance"]["final_relevance_score"],
                    item["relevance"]["vector_similarity"],
                ),
                reverse=True,
            )
            cross_version = cross_version[:accepted_limit]

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
            "query": query_text,
            "query_mode": (
                "structured" if query_analysis is not None else "raw_query_only"
            ),
            "matched_issues": matched,
            "related_issues": related,
            "no_close_match": not bool(matched),
            **(
                {"cross_version_matches": cross_version}
                if query_analysis is not None
                else {}
            ),
            **(
                {
                    "debug": {
                        "candidate_pool_size": len(candidates),
                        "accepted_count": len(matched),
                        "vector_candidates": [
                            {
                                "issue_number": issue.get("issue_number"),
                                "vector_score": float(issue.get("score") or 0.0),
                            }
                            for issue in candidates
                        ],
                        "reranking": [
                            {
                                "issue_number": issue.get("issue_number"),
                                **issue["relevance"],
                            }
                            for issue in candidates
                        ],
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

    def retrieve(
        self,
        query: str,
        limit: int = MAX_VECTOR_MATCHES,
        *,
        debug: bool = False,
    ) -> dict[str, Any]:
        """Backward-compatible raw-query-only search."""

        return self.search(
            query_text=query,
            query_analysis=None,
            limit=limit,
            debug=debug,
        )

    def _relevance_explanation(
        self,
        candidate: dict[str, Any],
        query_analysis: QueryAnalysis,
    ) -> dict[str, Any]:
        vector_similarity = float(candidate.get("score") or 0.0)
        requested_components = self._value_map(query_analysis.components)
        candidate_components = self._value_map(candidate.get("components", []))
        component_keys = set(requested_components) & set(candidate_components)
        matching_components = sorted(
            candidate_components[key] for key in component_keys
        )
        version_match_type, matching_versions, conflicting_versions = (
            self._compare_versions(
                query_analysis.kubernetes_version,
                candidate.get("kubernetes_versions", []),
            )
        )

        query_critical_error_map = self._critical_error_map(
            " ".join(query_analysis.error_messages)
        )
        query_critical_errors = set(query_critical_error_map)
        candidate_text = " ".join(
            [
                str(candidate.get("title") or ""),
                str(candidate.get("summary") or ""),
                *candidate.get("error_messages", []),
            ]
        )
        candidate_critical_error_map = self._critical_error_map(candidate_text)
        candidate_critical_errors = set(candidate_critical_error_map)
        matching_critical_errors = query_critical_errors & candidate_critical_errors
        requested_error_messages = self._value_map(query_analysis.error_messages)
        candidate_error_messages = self._value_map(
            candidate.get("error_messages", [])
        )
        matching_error_messages = (
            set(requested_error_messages) & set(candidate_error_messages)
        )

        diagnostic_terms = self._distinctive_term_map(
            query_analysis.symptom or ""
        )
        excluded_terms = set(query_critical_errors)
        excluded_terms.update(
            term
            for message in requested_error_messages
            for term in message.split()
        )
        component_terms = {
            term
            for component in requested_components
            for term in component.split()
        }
        excluded_terms.update(component_terms)
        diagnostic_terms = {
            key: original
            for key, original in diagnostic_terms.items()
            if key not in excluded_terms
        }
        candidate_terms = self._distinctive_term_map(candidate_text)
        matching_symptom_terms = sorted(
            candidate_terms[key]
            for key in set(diagnostic_terms) & set(candidate_terms)
        )
        matching_error_terms = sorted(
            {
                candidate_critical_error_map[key]
                for key in matching_critical_errors
            }
            | {
                candidate_error_messages[key]
                for key in matching_error_messages
            }
        )
        explicit_version_conflict = version_match_type == "conflict"
        exceptional = (
            vector_similarity >= self._settings.retrieval_exceptional_vector_score
        )
        rejection_reasons: list[str] = []
        if vector_similarity < self._settings.retrieval_min_vector_score:
            rejection_reasons.append("vector_score_below_minimum")
        if query_critical_errors and not matching_critical_errors:
            rejection_reasons.append("critical_error_mismatch")
        if requested_components and not component_keys and not exceptional:
            rejection_reasons.append("component_mismatch")
        if not (matching_error_terms or matching_symptom_terms):
            rejection_reasons.append("no_distinctive_symptom_overlap")
        final_score = min(
            1.0,
            vector_similarity * 0.65
            + (0.10 if matching_components else 0.0)
            + (
                0.10
                if version_match_type == "exact"
                else 0.05
                if version_match_type == "compatible_minor"
                else 0.0
            )
            + (0.15 if matching_error_terms or matching_symptom_terms else 0.0),
        )
        if final_score < self._settings.retrieval_min_final_relevance_score:
            rejection_reasons.append("final_score_below_minimum")
        passes_non_version_rules = not rejection_reasons
        cross_version_match = passes_non_version_rules and explicit_version_conflict
        if explicit_version_conflict:
            rejection_reasons.append("conflicting_kubernetes_version")
        accepted = passes_non_version_rules and not explicit_version_conflict
        return {
            "vector_similarity": vector_similarity,
            "matching_components": matching_components,
            "matching_versions": matching_versions,
            "conflicting_versions": conflicting_versions,
            "version_match_type": version_match_type,
            "matching_error_terms": matching_error_terms,
            "matching_symptom_terms": matching_symptom_terms,
            "version_conflict": explicit_version_conflict,
            "final_relevance_score": round(final_score, 6),
            "accepted": accepted,
            "cross_version_match": cross_version_match,
            "cross_version_explanation": (
                "Similar symptom in another Kubernetes version; its resolution "
                "must not be assumed to apply to the requested version."
                if cross_version_match
                else None
            ),
            "accepted_by": (
                "exceptional_vector"
                if exceptional
                else "vector_plus_diagnostic_overlap"
                if accepted
                else None
            ),
            "rejection_reasons": rejection_reasons,
        }

    def _raw_relevance_explanation(
        self, candidate: dict[str, Any]
    ) -> dict[str, Any]:
        vector_similarity = float(candidate.get("score") or 0.0)
        accepted = vector_similarity >= self._settings.retrieval_min_vector_score
        return {
            "vector_similarity": vector_similarity,
            "final_relevance_score": round(vector_similarity, 6),
            "accepted": accepted,
            "accepted_by": "raw_vector_score" if accepted else None,
            "rejection_reasons": (
                [] if accepted else ["vector_score_below_minimum"]
            ),
        }

    @staticmethod
    def _normalize_phrase(value: str) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", value.lower())).strip()

    @classmethod
    def _value_map(cls, values: list[str]) -> dict[str, str]:
        return {
            normalized: value.strip()
            for value in values
            if value and (normalized := cls._normalize_phrase(value))
        }

    @classmethod
    def _parse_version(
        cls, value: str | None
    ) -> tuple[int, int, int | None, bool] | None:
        if not value:
            return None
        match = re.search(
            r"(?:kubernetes|k8s)?\s*v?(\d+)[._-](\d+)"
            r"(?:[._-](\d+|x|\*))?",
            value,
            re.IGNORECASE,
        )
        if not match:
            return None
        patch = match.group(3)
        return (
            int(match.group(1)),
            int(match.group(2)),
            int(patch) if patch and patch.lower() not in {"x", "*"} else None,
            patch is not None,
        )

    @classmethod
    def _compare_versions(
        cls,
        requested: str | None,
        candidates: list[str],
    ) -> tuple[str, list[str], list[str]]:
        query_version = cls._parse_version(requested)
        parsed_candidates = [
            (value.strip(), parsed)
            for value in candidates
            if value and (parsed := cls._parse_version(value))
        ]
        if not query_version or not parsed_candidates:
            return "unknown", [], []

        exact: list[str] = []
        compatible: list[str] = []
        conflicting: list[str] = []
        for original, candidate in parsed_candidates:
            if candidate == query_version:
                exact.append(original)
            elif candidate[:2] == query_version[:2] and (
                candidate[2] is None
                or query_version[2] is None
            ):
                compatible.append(original)
            else:
                conflicting.append(original)
        if exact:
            return "exact", exact, conflicting
        if compatible:
            return "compatible_minor", compatible, conflicting
        return "conflict", [], conflicting

    @classmethod
    def _critical_errors(cls, value: str) -> set[str]:
        return set(cls._critical_error_map(value))

    @classmethod
    def _critical_error_map(cls, value: str) -> dict[str, str]:
        critical: dict[str, str] = {}
        for token in re.findall(r"[A-Za-z0-9_-]+", value):
            normalized = re.sub(r"[-_]", "", token.lower())
            if normalized in cls.KNOWN_CRITICAL_ERRORS:
                critical[normalized] = token
        for match in re.findall(
            r"\b[A-Za-z_][A-Za-z0-9_.]*(?:Exception|Error)\b", value
        ):
            normalized = cls._normalize_phrase(match).replace(" ", "")
            if normalized not in cls.GENERIC_ERRORS:
                critical[normalized] = match
        for token in re.findall(r"\b(?:[45]\d\d|0x[0-9a-fA-F]+)\b", value):
            critical[token.lower()] = token
        return critical

    @classmethod
    def _distinctive_terms(cls, value: str) -> set[str]:
        return set(cls._distinctive_term_map(value).values())

    @classmethod
    def _distinctive_term_map(cls, value: str) -> dict[str, str]:
        terms: dict[str, str] = {}
        for raw in re.findall(r"[A-Za-z0-9_.$/():\\-]+", value):
            original = raw.strip(".,:;[]{}<>'\"")
            if not original or len(original) < 4:
                continue
            comparison = original.lower()
            if comparison in cls.TERM_STOP_WORDS:
                continue
            if not cls._is_technical_token(original):
                stem = cls.STEMMER.stemWord(comparison)
                if stem in cls.GENERIC_SYMPTOM_STEMS:
                    continue
            terms[comparison] = original
        return terms

    @classmethod
    def _is_technical_token(cls, token: str) -> bool:
        compact = re.sub(r"[-_\s]+", "", token.lower())
        if compact in cls.KNOWN_CRITICAL_ERRORS:
            return True
        if re.fullmatch(r"v?\d+(?:[._-](?:\d+|x|\*))+", token, re.I):
            return True
        if re.search(r"[()/\\]|::|[._$-]", token):
            return True
        if any(character.isdigit() for character in token):
            return True
        if token.isupper() and len(token) > 1:
            return True
        return bool(re.search(r"[a-z][A-Z]|[A-Z]{2,}[a-z]", token))

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
