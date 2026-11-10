"""Deterministic allow-list validation for model-generated Cypher."""

import re
from dataclasses import dataclass
from typing import Any

from app.graph.schema import NODE_LABELS, RELATIONSHIP_TYPES


@dataclass(frozen=True)
class CypherValidationResult:
    valid: bool
    errors: tuple[str, ...]


class CypherValidator:
    MAX_QUERY_LENGTH = 4000
    MAX_RELATIONSHIP_PATTERNS = 4
    MAX_LIMIT = 100
    FORBIDDEN = re.compile(
        r"\b(?:CREATE|DELETE|DETACH|SET|REMOVE|MERGE|DROP|ALTER|RENAME|"
        r"GRANT|DENY|REVOKE|LOAD\s+CSV|FOREACH|CALL|USE|SHOW|TERMINATE)\b",
        re.IGNORECASE,
    )
    LABEL = re.compile(r"\([A-Za-z_]\w*\s*:\s*`?([A-Za-z_]\w*)`?")
    RELATIONSHIP = re.compile(r"\[[^\]]*:\s*`?([A-Za-z_]\w*)`?")
    PARAMETER = re.compile(r"\$([A-Za-z_]\w*)")
    LIMIT = re.compile(r"\bLIMIT\s+(?:(\d+)\b|\$([A-Za-z_]\w*))", re.IGNORECASE)
    INLINE_COMPARISON = re.compile(
        r"(?:=|<>|<=|>=|<|>)\s*(?:'[^']*'|\"[^\"]*\"|\d+(?:\.\d+)?)",
        re.IGNORECASE,
    )

    def validate(
        self, cypher: str, parameters: dict[str, Any]
    ) -> CypherValidationResult:
        errors: list[str] = []
        query = cypher.strip()
        if not query:
            errors.append("query is empty")
        if len(query) > self.MAX_QUERY_LENGTH:
            errors.append("query exceeds maximum length")
        if ";" in query or "//" in query or "/*" in query:
            errors.append("comments and multiple statements are not allowed")
        if self.FORBIDDEN.search(query):
            errors.append("query contains a write, procedure, or admin operation")
        if not re.search(r"\bMATCH\b", query, re.IGNORECASE):
            errors.append("query must contain MATCH")
        if not re.search(r"\bRETURN\b", query, re.IGNORECASE):
            errors.append("query must contain RETURN")

        labels = set(self.LABEL.findall(query))
        unknown_labels = sorted(labels - NODE_LABELS)
        if unknown_labels:
            errors.append("unknown node labels: " + ", ".join(unknown_labels))
        relationships = self.RELATIONSHIP.findall(query)
        unknown_relationships = sorted(set(relationships) - RELATIONSHIP_TYPES)
        if unknown_relationships:
            errors.append(
                "unknown relationship types: " + ", ".join(unknown_relationships)
            )
        if len(relationships) > self.MAX_RELATIONSHIP_PATTERNS:
            errors.append("query exceeds maximum relationship depth")

        referenced = set(self.PARAMETER.findall(query))
        missing = sorted(referenced - set(parameters))
        if missing:
            errors.append("missing parameters: " + ", ".join(missing))
        unused = sorted(set(parameters) - referenced)
        if unused:
            errors.append("unused parameters: " + ", ".join(unused))
        if self.INLINE_COMPARISON.search(query):
            errors.append("filter values must use parameters")

        limit = self.LIMIT.search(query)
        if limit is None:
            errors.append("query must enforce LIMIT")
        else:
            # A parameterized LIMIT is accepted only when it is a bounded integer.
            value = (
                int(limit.group(1)) if limit.group(1) else parameters.get(limit.group(2))
            )
            if not isinstance(value, int) or isinstance(value, bool):
                errors.append("LIMIT must be an integer")
            elif not 0 < value <= self.MAX_LIMIT:
                errors.append(f"LIMIT must be between 1 and {self.MAX_LIMIT}")
        return CypherValidationResult(not errors, tuple(errors))
