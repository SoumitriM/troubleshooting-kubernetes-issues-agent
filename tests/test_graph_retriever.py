from unittest.mock import MagicMock

from app.retrieval.graph_retriever import GraphRetriever


def test_retrieve_combines_vector_matches_with_bounded_graph_expansion() -> None:
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    matched_result = MagicMock()
    matched_result.data.return_value = [
        {
            "repository": "kubernetes/kubernetes",
            "issue_number": 10,
            "title": "Collector loops",
            "summary": "Valid owners are repeatedly checked",
            "url": "https://github.com/kubernetes/kubernetes/issues/10",
            "score": 0.94,
            "components": ["api machinery", "garbage collector"],
            "kubernetes_versions": ["v1.31"],
            "labels": ["bug"],
            "error_messages": ["owner reference missing"],
            "verified_resolution": {
                "resolution_found": True,
                "resolution_type": "fixed",
                "root_cause": "Stale owner reference",
                "resolution_summary": "Fixed by PR 12",
                "verified_steps": [],
                "confidence": "high",
                "evidence_urls": ["https://github.com/example/project/pull/12"],
            },
        },
        {
            "repository": "kubernetes/kubernetes",
            "issue_number": 11,
            "title": "Another collector issue",
            "summary": None,
            "url": "https://github.com/kubernetes/kubernetes/issues/11",
            "score": 0.88,
            "components": ["garbage collector"],
            "kubernetes_versions": ["v1.31"],
            "labels": ["kind/bug"],
            "error_messages": [],
            "verified_resolution": None,
        },
    ]
    related_result = MagicMock()
    related_result.data.return_value = [
        {
            "repository": "kubernetes/kubernetes",
            "issue_number": 9,
            "title": "Related issue",
            "summary": "Shares the collector component",
            "url": "https://github.com/kubernetes/kubernetes/issues/9",
            "shared_components": ["garbage collector"],
            "shared_versions": ["v1.31"],
            "verified_resolution": {
                "resolution_found": True,
                "resolution_type": "configuration",
                "root_cause": "Invalid setting",
                "resolution_summary": "Configuration corrected",
                "verified_steps": ["Update the setting"],
                "confidence": "medium",
                "evidence_urls": ["https://github.com/example/project/issues/9"],
            },
        }
    ]
    session.run.side_effect = [matched_result, related_result]
    embedder = MagicMock()
    embedder.encode_query.return_value = [0.0] * 384

    context = GraphRetriever(driver, embedder).retrieve("valid owners", 5)

    assert context["query"] == "valid owners"
    assert context["matched_issues"][0]["score"] == 0.94
    assert context["matched_issues"][0]["labels"] == ["bug"]
    assert context["matched_issues"][0]["verified_resolution"]["confidence"] == "high"
    assert context["matched_issues"][0]["verified_resolution"][
        "resolution_found"
    ] is True
    assert context["related_issues"][0]["issue_number"] == 9
    assert context["related_issues"][0]["verified_resolution"][
        "evidence_urls"
    ] == ["https://github.com/example/project/issues/9"]
    vector_call, expansion_call = session.run.call_args_list
    assert "db.index.vector.queryNodes" in vector_call.args[0]
    assert vector_call.kwargs["vector_limit"] == 5
    assert "collect(DISTINCT component.name)" in vector_call.args[0]
    assert "collect(DISTINCT version.version)" in vector_call.args[0]
    assert "collect(DISTINCT label.name)" in vector_call.args[0]
    assert "collect(DISTINCT error.message)" in vector_call.args[0]
    assert vector_call.args[0].count("CALL {") == 5
    assert "[:HAS_RESOLUTION]" in vector_call.args[0]
    assert "[:SUPPORTED_BY]" in vector_call.args[0]
    assert "verified_resolution" in expansion_call.args[0]
    assert expansion_call.kwargs["expansion_limit"] == 3
    assert expansion_call.kwargs["component_names"] == [
        "api machinery",
        "garbage collector",
    ]
    assert expansion_call.kwargs["version_names"] == ["v1.31"]
    assert expansion_call.kwargs["matched_issue_keys"] == [
        {"repository": "kubernetes/kubernetes", "issue_number": 10},
        {"repository": "kubernetes/kubernetes", "issue_number": 11},
    ]


def test_retrieve_caps_vector_matches_at_five() -> None:
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    result = MagicMock()
    result.data.return_value = []
    session.run.return_value = result
    embedder = MagicMock()
    embedder.encode_query.return_value = [0.0] * 384

    context = GraphRetriever(driver, embedder).retrieve("query", 100)

    assert context == {"query": "query", "matched_issues": [], "related_issues": []}
    assert session.run.call_args.kwargs["vector_limit"] == 5
    assert session.run.call_count == 1


def test_retrieve_skips_expansion_without_components_or_versions() -> None:
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    result = MagicMock()
    result.data.return_value = [
        {
            "repository": "kubernetes/kubernetes",
            "issue_number": 10,
            "components": [],
            "kubernetes_versions": [],
            "score": 0.9,
        }
    ]
    session.run.return_value = result
    embedder = MagicMock()
    embedder.encode_query.return_value = [0.0] * 384

    context = GraphRetriever(driver, embedder).retrieve("query")

    assert len(context["matched_issues"]) == 1
    assert context["related_issues"] == []
    assert session.run.call_count == 1
