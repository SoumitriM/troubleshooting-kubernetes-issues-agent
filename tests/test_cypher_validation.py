from app.graph.cypher_validation import CypherValidator


def test_accepts_parameterized_query_against_actual_schema() -> None:
    result = CypherValidator().validate(
        """
        MATCH (i:Issue)-[:AFFECTS_COMPONENT]->(c:Component)
        MATCH (i)-[:REPORTED_IN_VERSION]->(v:KubernetesVersion)
        WHERE c.name = $component AND v.version = $version
        RETURN i.title AS issue, count(*) AS reports
        ORDER BY reports DESC LIMIT 20
        """,
        {"component": "kubelet", "version": "v1.35"},
    )
    assert result.valid is True
    assert result.errors == ()


def test_rejects_malicious_write_query() -> None:
    result = CypherValidator().validate(
        "MATCH (i:Issue) DETACH DELETE i RETURN i LIMIT 10", {}
    )
    assert result.valid is False
    assert any("write" in error for error in result.errors)


def test_rejects_unknown_relationship_name() -> None:
    result = CypherValidator().validate(
        "MATCH (i:Issue)-[:MADE_UP]->(c:Component) RETURN c LIMIT 10", {}
    )
    assert result.valid is False
    assert any("MADE_UP" in error for error in result.errors)


def test_rejects_unparameterized_filters_and_missing_parameters() -> None:
    validator = CypherValidator()
    inline = validator.validate(
        "MATCH (c:Component) WHERE c.name = 'kubelet' RETURN c LIMIT 10", {}
    )
    missing = validator.validate(
        "MATCH (c:Component) WHERE c.name = $component RETURN c LIMIT 10", {}
    )
    assert any("parameters" in error for error in inline.errors)
    assert any("missing parameters" in error for error in missing.errors)


def test_parameterized_limit_must_be_a_bounded_integer() -> None:
    validator = CypherValidator()
    query = "MATCH (i:Issue) RETURN i.issue_number LIMIT $limit"

    assert validator.validate(query, {"limit": 50}).valid
    assert "LIMIT must be between 1 and 100" in validator.validate(query, {"limit": 500}).errors
    assert "LIMIT must be an integer" in validator.validate(query, {"limit": "50"}).errors
