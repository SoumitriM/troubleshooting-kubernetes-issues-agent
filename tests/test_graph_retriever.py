from unittest.mock import MagicMock

from app.agent.nodes import AgentSettings, IncidentNodes
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
            "verified_disposition": {
                "resolution_found": True,
                "disposition_verified": True,
                "actionable_fix_available": True,
                "resolution_type": "fixed",
                "root_cause": "Stale owner reference",
                "resolution_summary": "Fixed by PR 12",
                "verified_steps": ["Apply PR 12"],
                "confidence": "high",
                "evidence_urls": ["https://github.com/example/project/pull/12"],
            },
            "actionable_fix": {
                "resolution_type": "fixed",
                "resolution_summary": "Fixed by PR 12",
                "verified_steps": ["Apply PR 12"],
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
            "verified_disposition": None,
            "actionable_fix": None,
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
            "verified_disposition": {
                "resolution_found": True,
                "disposition_verified": True,
                "actionable_fix_available": True,
                "resolution_type": "configuration",
                "root_cause": "Invalid setting",
                "resolution_summary": "Configuration corrected",
                "verified_steps": ["Update the setting"],
                "confidence": "medium",
                "evidence_urls": ["https://github.com/example/project/issues/9"],
            },
            "actionable_fix": {
                "resolution_type": "configuration",
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
    assert context["matched_issues"][0]["verified_disposition"]["confidence"] == "high"
    assert context["matched_issues"][0]["verified_disposition"][
        "resolution_found"
    ] is True
    assert context["matched_issues"][0]["actionable_fix"]["verified_steps"] == [
        "Apply PR 12"
    ]
    assert context["related_issues"][0]["issue_number"] == 9
    assert context["related_issues"][0]["verified_disposition"][
        "evidence_urls"
    ] == ["https://github.com/example/project/issues/9"]
    vector_call, expansion_call = session.run.call_args_list
    assert "db.index.vector.queryNodes" in vector_call.args[0]
    assert vector_call.kwargs["vector_limit"] == 20
    assert "collect(DISTINCT component.name)" in vector_call.args[0]
    assert "collect(DISTINCT version.version)" in vector_call.args[0]
    assert "collect(DISTINCT label.name)" in vector_call.args[0]
    assert "collect(DISTINCT error.message)" in vector_call.args[0]
    assert vector_call.args[0].count("CALL (issue) {") == 5
    assert "CALL (issue) {\n        WITH issue" not in vector_call.args[0]
    assert "[:HAS_RESOLUTION]" in vector_call.args[0]
    assert "[:SUPPORTED_BY]" in vector_call.args[0]
    assert "verified_disposition" in expansion_call.args[0]
    assert "actionable_fix" in expansion_call.args[0]
    assert "['fixed', 'configuration']" in vector_call.args[0]
    assert "CALL (issue) {" in expansion_call.args[0]
    assert "CALL {\n        WITH issue" not in expansion_call.args[0]
    assert expansion_call.kwargs["expansion_limit"] == 3
    assert expansion_call.kwargs["component_names"] == [
        "api machinery",
        "garbage collector",
    ]
    assert expansion_call.kwargs["version_names"] == ["v1.31"]
    assert expansion_call.kwargs["expansion_sources"] == [
        {
            "components": ["api machinery", "garbage collector"],
            "versions": ["v1.31"],
        }
    ]
    assert expansion_call.kwargs["matched_issue_keys"] == [
        {"repository": "kubernetes/kubernetes", "issue_number": 10},
    ]
    assert context["matched_issues"][0]["relevance"] == {
        "vector_similarity": 0.94,
        "matching_components": [],
        "matching_versions": [],
        "matching_error_terms": ["owners", "valid"],
        "final_relevance_score": 0.858,
        "accepted": True,
        "accepted_by": "exceptional_vector",
    }
    assert "size(shared_components) >= 2" in expansion_call.args[0]
    assert "size(shared_versions) >= 1" in expansion_call.args[0]


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
    assert session.run.call_args.kwargs["vector_limit"] == 20
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


def test_issue_141293_is_disposition_context_not_an_actionable_solution() -> None:
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    result = MagicMock()
    result.data.return_value = [
        {
            "repository": "kubernetes/kubernetes",
            "issue_number": 141293,
            "title": "Working as designed",
            "summary": "Closed without a code fix",
            "url": "https://github.com/kubernetes/kubernetes/issues/141293",
            "score": 0.92,
            "components": [],
            "kubernetes_versions": [],
            "labels": ["support"],
            "error_messages": [],
            "verified_disposition": {
                "resolution_found": True,
                "disposition_verified": True,
                "actionable_fix_available": False,
                "resolution_type": "closed_without_fix",
                "root_cause": None,
                "resolution_summary": "Closed as working as designed",
                "verified_steps": [],
                "confidence": "high",
                "evidence_urls": [
                    "https://github.com/kubernetes/kubernetes/issues/141293#issuecomment-1"
                ],
            },
            "actionable_fix": None,
        }
    ]
    session.run.return_value = result
    embedder = MagicMock()
    embedder.encode_query.return_value = [0.0] * 384

    context = GraphRetriever(driver, embedder).retrieve("possible solution")

    issue = context["matched_issues"][0]
    assert issue["issue_number"] == 141293
    assert issue["verified_disposition"]["disposition_verified"] is True
    assert issue["verified_disposition"]["resolution_type"] == "closed_without_fix"
    assert issue["actionable_fix"] is None


def test_crashloopbackoff_excludes_unrelated_generic_kubelet_candidate() -> None:
    candidates = [
        {
            "repository": "kubernetes/kubernetes",
            "issue_number": 500,
            "title": "Generic kubelet maintenance",
            "summary": "Routine node maintenance discussion",
            "url": "https://github.com/kubernetes/kubernetes/issues/500",
            "score": 0.82,
            "components": ["kubelet"],
            "kubernetes_versions": ["v1.34"],
            "labels": [],
            "error_messages": [],
            "verified_disposition": None,
            "actionable_fix": None,
        },
        {
            "repository": "kubernetes/kubernetes",
            "issue_number": 501,
            "title": "Pods enter CrashLoopBackOff after kubelet restart",
            "summary": "CrashLoopBackOff follows a kubelet restart",
            "url": "https://github.com/kubernetes/kubernetes/issues/501",
            "score": 0.78,
            "components": ["kubelet"],
            "kubernetes_versions": ["v1.35"],
            "labels": ["bug"],
            "error_messages": ["CrashLoopBackOff"],
            "verified_disposition": None,
            "actionable_fix": None,
        },
    ]
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    candidate_result = MagicMock()
    candidate_result.data.return_value = candidates
    related_result = MagicMock()
    related_result.data.return_value = []
    session.run.side_effect = [candidate_result, related_result]
    embedder = MagicMock()
    embedder.encode_query.return_value = [0.0] * 384

    context = GraphRetriever(driver, embedder).retrieve(
        "Pods enter CrashLoopBackOff after kubelet restart on Kubernetes v1.35",
        components=["kubelet"],
        kubernetes_version="v1.35",
        error_messages=["CrashLoopBackOff"],
        symptom="Pods enter CrashLoopBackOff after kubelet restart",
        debug=True,
    )

    assert [issue["issue_number"] for issue in context["matched_issues"]] == [501]
    assert context["matched_issues"][0]["relevance"]["matching_versions"] == [
        "v1.35"
    ]
    assert context["matched_issues"][0]["relevance"]["matching_error_terms"] == [
        "crashloopbackoff",
        "restart",
    ]
    assert context["debug"]["rejected_candidates"][0]["issue_number"] == 500
    returned_urls = {
        issue["url"]
        for issue in [
            *context["matched_issues"],
            *context["related_issues"],
        ]
    }
    assert "https://github.com/kubernetes/kubernetes/issues/500" not in returned_urls
    findings = IncidentNodes(
        MagicMock(),
        MagicMock(),
        AgentSettings(_env_file=None),
    ).generate_findings({"retrieved_context": context})
    assert "https://github.com/kubernetes/kubernetes/issues/500" not in findings[
        "citations"
    ]


def test_exact_garbage_collector_query_accepts_issue_141382() -> None:
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    candidate_result = MagicMock()
    candidate_result.data.return_value = [
        {
            "repository": "kubernetes/kubernetes",
            "issue_number": 141382,
            "title": "Garbage collector keeps processing objects with valid owners",
            "summary": "Objects with valid owners are repeatedly processed",
            "url": "https://github.com/kubernetes/kubernetes/issues/141382",
            "score": 0.76,
            "components": ["garbage collector"],
            "kubernetes_versions": [],
            "labels": ["bug"],
            "error_messages": [],
            "verified_disposition": None,
            "actionable_fix": None,
        }
    ]
    related_result = MagicMock()
    related_result.data.return_value = []
    session.run.side_effect = [candidate_result, related_result]
    embedder = MagicMock()
    embedder.encode_query.return_value = [0.0] * 384

    context = GraphRetriever(driver, embedder).retrieve(
        "garbage collector keeps processing objects with valid owners",
        components=["garbage collector"],
        symptom="keeps processing objects with valid owners",
    )

    assert [issue["issue_number"] for issue in context["matched_issues"]] == [
        141382
    ]
    relevance = context["matched_issues"][0]["relevance"]
    assert relevance["matching_components"] == ["garbage collector"]
    assert relevance["accepted_by"] == "vector_plus_diagnostic_overlap"


def test_graph_expansion_query_requires_strict_shared_context() -> None:
    query = GraphRetriever.RELATED_ISSUES_QUERY

    assert "size(shared_components) >= 2" in query
    assert "size(shared_components) >= 1 AND size(shared_versions) >= 1" in query
    assert "source IN $expansion_sources" in query
    assert "LIMIT $expansion_limit" in query
