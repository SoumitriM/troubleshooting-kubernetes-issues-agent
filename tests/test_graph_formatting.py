from app.graph.formatting import format_graph_answer, query_limit


def test_single_value_is_rendered_as_a_labelled_figure() -> None:
    answer, citations = format_graph_answer([{"issue_count": 3}])

    assert answer == "**Issue count:** 3"
    assert citations == []


def test_aggregate_rows_read_as_counts_and_report_ties_beyond_the_limit() -> None:
    rows = [
        {"component": "kubelet", "issue_count": 13},
        {"component": "dra", "issue_count": 10},
        {"component": "sig-node", "issue_count": 10},
    ]

    answer, _ = format_graph_answer(rows, display_limit=2, probed=True)

    assert answer.startswith("Top 2 results (more exist, including at least one tied with the last shown):")
    assert "- kubelet: 13 issues" in answer
    assert "sig-node" not in answer


def test_issue_rows_are_deduplicated_linked_and_cited() -> None:
    row = {
        "issue_number": 140640,
        "title": "Serial job failing",
        "url": "https://github.com/kubernetes/kubernetes/issues/140640",
        "discussion_count": 16,
    }

    answer, citations = format_graph_answer([row, dict(row)])

    assert answer == "Found 1 issue:\n- **#140640** Serial job failing (16 comments)"
    assert citations == ["https://github.com/kubernetes/kubernetes/issues/140640"]


def test_model_query_at_its_limit_may_have_more_rows() -> None:
    rows = [{"component": f"c{n}", "issue_count": 1} for n in range(5)]

    answer, _ = format_graph_answer(rows, display_limit=5, probed=False)

    assert "the query limit was 5, so more may exist" in answer
    assert query_limit("MATCH (i:Issue) RETURN i LIMIT 5") == 5


def test_plural_count_columns_are_not_double_pluralised() -> None:
    answer, _ = format_graph_answer(
        [{"issue_number": 1, "title": "t", "comments_count": 4},
         {"issue_number": 2, "title": "u", "comments_count": 1}]
    )

    assert "(4 comments)" in answer and "(1 comment)" in answer


def test_issue_rows_are_deduplicated_by_issue_number() -> None:
    answer, citations = format_graph_answer([
        {"issue_number": 7, "title": "t", "component": "a", "url": "https://github.com/k/k/issues/7"},
        {"issue_number": 7, "title": "t", "component": "b", "url": "https://github.com/k/k/issues/7"},
    ])

    assert answer.startswith("Found 1 issue:")
    assert citations == ["https://github.com/k/k/issues/7"]


def test_links_in_any_column_become_citations_not_text() -> None:
    answer, citations = format_graph_answer([{
        "issue_number": 141080,
        "title": "Failed to create master instance",
        "issue_url": "https://github.com/kubernetes/kubernetes/issues/141080",
        "pull_requests": ["https://github.com/kubernetes/kubernetes/pull/141082"],
    }])

    assert "https://" not in answer
    assert citations == [
        "https://github.com/kubernetes/kubernetes/issues/141080",
        "https://github.com/kubernetes/kubernetes/pull/141082",
    ]
