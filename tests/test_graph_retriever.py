from unittest.mock import MagicMock

import pytest

from app.agent.nodes import AgentSettings, IncidentNodes
from app.agent.state import QueryAnalysis
from app.retrieval.graph_retriever import GraphRetriever


def analysis(
    *,
    symptom: str | None = None,
    components: list[str] | None = None,
    kubernetes_version: str | None = None,
    error_messages: list[str] | None = None,
) -> QueryAnalysis:
    return QueryAnalysis(
        symptom=symptom,
        components=components or [],
        kubernetes_version=kubernetes_version,
        error_messages=error_messages or [],
        has_sufficient_details=True,
        missing_details=[],
    )


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

    context = GraphRetriever(driver, embedder).search(
        query_text="valid owners",
        query_analysis=analysis(symptom="valid owners"),
        limit=5,
    )

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
        "conflicting_versions": [],
        "version_match_type": "unknown",
        "matching_error_terms": [],
        "matching_symptom_terms": ["Valid", "owners"],
        "version_conflict": False,
        "final_relevance_score": 0.761,
        "accepted": True,
        "cross_version_match": False,
        "cross_version_explanation": None,
        "accepted_by": "exceptional_vector",
        "rejection_reasons": [],
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

    assert context == {
        "query": "query",
        "query_mode": "raw_query_only",
        "matched_issues": [],
        "related_issues": [],
        "no_close_match": True,
    }
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
            "title": "Distinctive diagnostic signature",
            "summary": "Distinctive diagnostic signature was observed",
            "components": [],
            "kubernetes_versions": [],
            "score": 0.9,
        }
    ]
    session.run.return_value = result
    embedder = MagicMock()
    embedder.encode_query.return_value = [0.0] * 384

    context = GraphRetriever(driver, embedder).retrieve(
        "distinctive diagnostic signature"
    )

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

    context = GraphRetriever(driver, embedder).retrieve("closed without code fix")

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

    context = GraphRetriever(driver, embedder).search(
        query_text="Pods enter CrashLoopBackOff after kubelet restart on Kubernetes v1.35",
        query_analysis=analysis(
            components=["kubelet"],
            kubernetes_version="v1.35",
            error_messages=["CrashLoopBackOff"],
            symptom="Pods enter CrashLoopBackOff after kubelet restart",
        ),
        debug=True,
    )

    assert [issue["issue_number"] for issue in context["matched_issues"]] == [501]
    assert context["matched_issues"][0]["relevance"]["matching_versions"] == [
        "v1.35"
    ]
    assert context["matched_issues"][0]["relevance"]["matching_error_terms"] == [
        "CrashLoopBackOff",
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


def test_crashloopbackoff_returns_no_match_when_candidates_lack_exact_status() -> None:
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    result = MagicMock()
    result.data.return_value = [
        {
            "repository": "kubernetes/kubernetes",
            "issue_number": 500,
            "title": "Kubelet restarts pods",
            "summary": "Pods restart during node maintenance",
            "url": "https://github.com/kubernetes/kubernetes/issues/500",
            "score": 0.96,
            "components": ["kubelet"],
            "kubernetes_versions": ["v1.35"],
            "error_messages": [],
        }
    ]
    session.run.return_value = result
    embedder = MagicMock()
    embedder.encode_query.return_value = [0.0] * 384

    context = GraphRetriever(driver, embedder).search(
        query_text="Pods enter CrashLoopBackOff after kubelet restart on Kubernetes v1.35",
        query_analysis=analysis(
            components=["kubelet"],
            kubernetes_version="Kubernetes 1_35",
            error_messages=["CrashLoopBackOff"],
            symptom="Pods enter CrashLoopBackOff after kubelet restart",
        ),
        debug=True,
    )

    assert context["matched_issues"] == []
    assert context["related_issues"] == []
    assert context["no_close_match"] is True
    assert session.run.call_count == 1
    assert "critical_error_mismatch" in context["debug"]["rejected_candidates"][0][
        "relevance"
    ]["rejection_reasons"]


def test_structured_analysis_returns_strong_version_conflict_separately() -> None:
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    result = MagicMock()
    result.data.return_value = [
        {
            "repository": "kubernetes/kubernetes",
            "issue_number": 140743,
            "title": "Kubelet reports CrashLoopBackOff after restart",
            "summary": "A container enters CrashLoopBackOff after kubelet restart",
            "url": "https://github.com/kubernetes/kubernetes/issues/140743",
            "score": 0.82,
            "components": ["kubelet"],
            "kubernetes_versions": ["v1.29.15"],
            "error_messages": ["CrashLoopBackOff"],
        }
    ]
    session.run.return_value = result
    embedder = MagicMock()
    embedder.encode_query.return_value = [0.0] * 384
    query_analysis = analysis(
        symptom="Pods enter CrashLoopBackOff after the kubelet restarts",
        components=["kubelet"],
        kubernetes_version="v1.35",
        error_messages=["CrashLoopBackOff"],
    )

    context = GraphRetriever(driver, embedder).search(
        query_text=(
            "Pods enter CrashLoopBackOff after the kubelet restarts on "
            "Kubernetes v1.35"
        ),
        query_analysis=query_analysis,
        debug=True,
    )

    assert context["query_mode"] == "structured"
    assert context["matched_issues"] == []
    assert context["related_issues"] == []
    assert context["no_close_match"] is True
    assert session.run.call_count == 1
    assert [issue["issue_number"] for issue in context["cross_version_matches"]] == [
        140743
    ]
    relevance = context["cross_version_matches"][0]["relevance"]
    assert relevance["matching_components"] == ["kubelet"]
    assert relevance["matching_error_terms"] == ["CrashLoopBackOff"]
    assert "kubelet" not in [
        term.lower() for term in relevance["matching_symptom_terms"]
    ]
    assert relevance["version_match_type"] == "conflict"
    assert relevance["matching_versions"] == []
    assert relevance["conflicting_versions"] == ["v1.29.15"]
    assert relevance["version_conflict"] is True
    assert relevance["accepted"] is False
    assert relevance["cross_version_match"] is True
    assert "another Kubernetes version" in relevance["cross_version_explanation"]
    assert "conflicting_kubernetes_version" in relevance["rejection_reasons"]


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
        },
        {
            "repository": "kubernetes/kubernetes",
            "issue_number": 700,
            "title": "Kubelet restart handling",
            "summary": "Node service restarts are delayed",
            "url": "https://github.com/kubernetes/kubernetes/issues/700",
            "score": 0.88,
            "components": ["kubelet"],
            "kubernetes_versions": [],
            "error_messages": [],
        },
        {
            "repository": "kubernetes/kubernetes",
            "issue_number": 701,
            "title": "DRA admin access",
            "summary": "Namespace labels enable admin access",
            "url": "https://github.com/kubernetes/kubernetes/issues/701",
            "score": 0.86,
            "components": ["DRA"],
            "kubernetes_versions": [],
            "error_messages": [],
        },
        {
            "repository": "kubernetes/kubernetes",
            "issue_number": 702,
            "title": "Controller-manager leader election",
            "summary": "Leader election lease is renewed",
            "url": "https://github.com/kubernetes/kubernetes/issues/702",
            "score": 0.84,
            "components": ["controller-manager"],
            "kubernetes_versions": [],
            "error_messages": [],
        },
    ]
    related_result = MagicMock()
    related_result.data.return_value = []
    session.run.side_effect = [candidate_result, related_result]
    embedder = MagicMock()
    embedder.encode_query.return_value = [0.0] * 384

    context = GraphRetriever(driver, embedder).search(
        query_text="garbage collector keeps processing objects with valid owners",
        query_analysis=analysis(
            components=["garbage-collector"],
            symptom="keeps processing objects with valid owners",
        ),
    )

    assert [issue["issue_number"] for issue in context["matched_issues"]] == [
        141382
    ]
    relevance = context["matched_issues"][0]["relevance"]
    assert relevance["matching_components"] == ["garbage collector"]
    assert relevance["accepted_by"] == "vector_plus_diagnostic_overlap"
    assert context["no_close_match"] is False
    assert len(context["matched_issues"]) == 1


def test_relative_score_filter_does_not_fill_the_top_five() -> None:
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    candidate_result = MagicMock()
    candidate_result.data.return_value = [
        {
            "repository": "kubernetes/kubernetes",
            "issue_number": 1,
            "title": "Distinctive allocator exhaustion",
            "summary": "Allocator exhaustion blocks allocation",
            "url": "https://github.com/kubernetes/kubernetes/issues/1",
            "score": 0.90,
            "components": [],
            "kubernetes_versions": [],
            "error_messages": [],
        },
        {
            "repository": "kubernetes/kubernetes",
            "issue_number": 2,
            "title": "Allocator exhaustion",
            "summary": "Allocator exhaustion appears in a different path",
            "url": "https://github.com/kubernetes/kubernetes/issues/2",
            "score": 0.65,
            "components": [],
            "kubernetes_versions": [],
            "error_messages": [],
        },
    ]
    session.run.return_value = candidate_result
    embedder = MagicMock()
    embedder.encode_query.return_value = [0.0] * 384

    context = GraphRetriever(driver, embedder).retrieve(
        "distinctive allocator exhaustion",
        debug=True,
    )

    assert [issue["issue_number"] for issue in context["matched_issues"]] == [1]
    rejected = context["debug"]["rejected_candidates"]
    assert rejected[0]["issue_number"] == 2
    assert "outside_relative_score_window" in rejected[0]["relevance"][
        "rejection_reasons"
    ]
    assert context["query_mode"] == "raw_query_only"
    assert "cross_version_matches" not in context
    for issue in context["matched_issues"]:
        assert "matching_components" not in issue["relevance"]
        assert "matching_versions" not in issue["relevance"]
        assert "matching_error_terms" not in issue["relevance"]
        assert "matching_symptom_terms" not in issue["relevance"]


def test_graph_expansion_query_requires_strict_shared_context() -> None:
    query = GraphRetriever.RELATED_ISSUES_QUERY

    assert "size(shared_components) >= 2" in query
    assert "size(shared_components) >= 1 AND size(shared_versions) >= 1" in query
    assert "source IN $expansion_sources" in query
    assert "LIMIT $expansion_limit" in query


@pytest.mark.parametrize(
    ("candidate", "expected_type", "matching", "conflicting"),
    [
        ("v1.35", "exact", ["v1.35"], []),
        ("v1.35.x", "compatible_minor", ["v1.35.x"], []),
        ("v1.35.2", "compatible_minor", ["v1.35.2"], []),
        ("v1.36.2-eks-*", "conflict", [], ["v1.36.2-eks-*"]),
        ("v1.29.15", "conflict", [], ["v1.29.15"]),
        (None, "unknown", [], []),
    ],
)
def test_version_comparison_distinguishes_exact_compatible_conflict_and_unknown(
    candidate: str | None,
    expected_type: str,
    matching: list[str],
    conflicting: list[str],
) -> None:
    candidates = [candidate] if candidate else []

    result = GraphRetriever._compare_versions("v1.35", candidates)

    assert result == (expected_type, matching, conflicting)


def test_vendor_specific_build_is_compatible_build_not_exact() -> None:
    result = GraphRetriever._compare_versions(
        "v1.36.2",
        ["v1.36.2-eks-*"],
    )

    assert result == ("compatible_build", ["v1.36.2-eks-*"], [])


def test_vendor_specific_build_remains_an_accepted_structured_match() -> None:
    retriever = GraphRetriever(MagicMock(), MagicMock())
    relevance = retriever._relevance_explanation(
        {
            "score": 0.8,
            "title": "Kubelet startup latency",
            "summary": None,
            "components": ["kubelet"],
            "kubernetes_versions": ["v1.36.2-eks-*"],
            "error_messages": [],
        },
        analysis(
            symptom="kubelet startup latency",
            components=["kubelet"],
            kubernetes_version="v1.36.2",
        ),
    )

    assert relevance["version_match_type"] == "compatible_build"
    assert relevance["matching_versions"] == ["v1.36.2-eks-*"]
    assert relevance["conflicting_versions"] == []
    assert relevance["version_conflict"] is False
    assert relevance["accepted"] is True


def test_generic_symptom_inflections_are_not_distinctive() -> None:
    generic = {
        "restart",
        "restarts",
        "restarted",
        "restarting",
        "fail",
        "fails",
        "failed",
        "failing",
        "failure",
        "work",
        "works",
        "working",
        "break",
        "breaks",
        "breaking",
        "broken",
    }

    assert GraphRetriever._distinctive_terms(" ".join(generic)) == set()


def test_generic_symptoms_are_filtered_by_snowball_stem() -> None:
    stems = {
        word: GraphRetriever.STEMMER.stemWord(word)
        for word in [
            "restart",
            "restarts",
            "restarted",
            "restarting",
            "fail",
            "fails",
            "failed",
            "failing",
            "failure",
            "work",
            "works",
            "working",
            "break",
            "breaks",
            "breaking",
            "broken",
        ]
    }

    assert stems["restart"] == stems["restarts"] == stems["restarted"]
    assert stems["restart"] == stems["restarting"]
    assert stems["fail"] == stems["fails"] == stems["failed"] == stems["failing"]
    assert stems["work"] == stems["works"] == stems["working"]
    assert stems["break"] == stems["breaks"] == stems["breaking"]
    assert stems["failure"] in GraphRetriever.GENERIC_SYMPTOM_STEMS
    assert stems["broken"] in GraphRetriever.GENERIC_SYMPTOM_STEMS


def test_technical_tokens_are_not_stemmed_and_keep_original_display() -> None:
    value = (
        "CrashLoopBackOff OOMKilled ImagePullBackOff v1.35 "
        "/var/log/kubelet.log restart_handler $restart RestartWorker PROCESSING"
    )

    terms = GraphRetriever._distinctive_term_map(value)

    assert terms["crashloopbackoff"] == "CrashLoopBackOff"
    assert terms["oomkilled"] == "OOMKilled"
    assert terms["imagepullbackoff"] == "ImagePullBackOff"
    assert terms["v1.35"] == "v1.35"
    assert terms["/var/log/kubelet.log"] == "/var/log/kubelet.log"
    assert terms["restart_handler"] == "restart_handler"
    assert terms["$restart"] == "$restart"
    assert terms["restartworker"] == "RestartWorker"
    assert terms["processing"] == "PROCESSING"


def test_static_stopwords_are_removed_but_kubernetes_terms_are_preserved() -> None:
    stopwords = (
        "have has had be is are was were do does did with after before when "
        "while even though their"
    )
    terms = GraphRetriever._distinctive_term_map(
        f"{stopwords} Kubernetes kubelet Pod CrashLoopBackOff"
    )

    for stopword in stopwords.split():
        assert stopword not in terms
    assert terms["kubernetes"] == "Kubernetes"
    assert terms["kubelet"] == "kubelet"
    assert terms["pod"] == "Pod"
    assert terms["crashloopbackoff"] == "CrashLoopBackOff"


def test_have_cannot_contribute_to_relevance_score() -> None:
    retriever = GraphRetriever(MagicMock(), MagicMock())
    relevance = retriever._relevance_explanation(
        {
            "score": 0.8,
            "title": "have",
            "summary": None,
            "components": ["kubelet"],
            "kubernetes_versions": [],
            "error_messages": [],
        },
        analysis(symptom="have", components=["kubelet"]),
    )

    assert "have" not in [
        term.lower() for term in relevance["matching_symptom_terms"]
    ]
    assert relevance["matching_symptom_terms"] == []
    assert relevance["final_relevance_score"] == 0.62
    assert relevance["accepted"] is False
    assert "no_distinctive_symptom_overlap" in relevance["rejection_reasons"]


def test_matching_symptom_terms_preserve_candidate_spelling() -> None:
    retriever = GraphRetriever(MagicMock(), MagicMock())
    relevance = retriever._relevance_explanation(
        {
            "score": 0.8,
            "title": "Objects remain PROCESSING",
            "summary": None,
            "components": [],
            "kubernetes_versions": [],
            "error_messages": [],
        },
        analysis(symptom="objects remain processing"),
    )

    assert relevance["matching_symptom_terms"] == ["Objects", "PROCESSING", "remain"]


def test_error_and_symptom_matches_are_separate_and_restarts_is_generic() -> None:
    retriever = GraphRetriever(MagicMock(), MagicMock())
    candidate = {
        "score": 0.82,
        "title": "CrashLoopBackOff during startup after kubelet restarts",
        "summary": "Container processing reaches a timeout",
        "components": ["kubelet"],
        "kubernetes_versions": ["v1.35"],
        "error_messages": ["CrashLoopBackOff"],
    }

    relevance = retriever._relevance_explanation(
        candidate,
        analysis(
            components=["kubelet"],
            kubernetes_version="v1.35",
            error_messages=["CrashLoopBackOff"],
            symptom="startup timeout after kubelet restarts",
        ),
    )

    assert relevance["matching_error_terms"] == ["CrashLoopBackOff"]
    assert relevance["matching_symptom_terms"] == ["startup", "timeout"]
    assert "restarts" not in relevance["matching_error_terms"]
    assert "restart" not in relevance["matching_symptom_terms"]
    assert relevance["version_match_type"] == "exact"


def test_exact_structured_error_message_is_an_error_match_not_a_symptom() -> None:
    retriever = GraphRetriever(MagicMock(), MagicMock())
    relevance = retriever._relevance_explanation(
        {
            "score": 0.8,
            "title": "Registry access during startup",
            "summary": "The image cannot be downloaded",
            "components": ["kubelet"],
            "kubernetes_versions": [],
            "error_messages": ["connection refused"],
        },
        analysis(
            components=["kubelet"],
            error_messages=["connection refused"],
            symptom="image startup",
        ),
    )

    assert relevance["matching_error_terms"] == ["connection refused"]
    assert "connection" not in relevance["matching_symptom_terms"]
    assert "refused" not in relevance["matching_symptom_terms"]
