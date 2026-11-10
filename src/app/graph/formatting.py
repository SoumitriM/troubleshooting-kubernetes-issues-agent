"""Render graph-analytics rows as a readable answer without a model.

The answer contains only values from the rows, so analytics answers cannot
hallucinate. Issue rows become linked lines and citations; aggregate rows
become "name: N issues" lines. When the query fetched one row beyond the
display limit, the answer states that more results exist and whether the next
one ties with the last shown.
"""

import json
import re
from typing import Any


ISSUE_NUMBER = "issue_number"
URL_FIELDS = ("url", "html_url")
TEXT_FIELDS = ("title", "summary")
LIMIT = re.compile(r"\bLIMIT\s+(\d+)\s*$", re.IGNORECASE)


def query_limit(cypher: str) -> int | None:
    match = LIMIT.search(cypher.strip())
    return int(match[1]) if match else None


def format_graph_answer(
    rows: list[dict[str, Any]],
    *,
    display_limit: int | None = None,
    probed: bool = False,
) -> tuple[str, list[str]]:
    """Return (answer, citations) for graph rows.

    ``probed`` means the query asked for ``display_limit + 1`` rows, so an
    extra row proves the list was cut short; otherwise a full page only
    suggests more may exist.
    """

    unique = _unique_rows(rows)
    if not unique:
        return "The knowledge graph returned no matching results.", []
    shown = unique[:display_limit] if display_limit else unique
    hidden = unique[len(shown):]

    if len(shown) == 1 and len(shown[0]) == 1:
        (column, value), = shown[0].items()
        return f"**{_label(column)}:** {_value(value)}", []

    lines = [_issue_line(row) if ISSUE_NUMBER in row else _aggregate_line(row) for row in shown]
    noun = "issue" if all(ISSUE_NUMBER in row for row in shown) else "result"
    plural = f"{noun}{'' if len(shown) == 1 else 's'}"
    header = f"Found {len(shown)} {plural}"
    if hidden:
        header = f"Top {len(shown)} {plural} (more exist"
        if _sort_value(hidden[0]) is not None and _sort_value(hidden[0]) == _sort_value(shown[-1]):
            header += ", including at least one tied with the last shown"
        header += ")"
    elif probed is False and display_limit and len(shown) == display_limit:
        header += f" (the query limit was {display_limit}, so more may exist)"
    citations = list(dict.fromkeys(url for row in shown for url in _urls(row)))
    return header + ":\n" + "\n".join(lines), citations


def _is_url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def _urls(row: dict[str, Any]) -> list[str]:
    """Links in any column (model-written queries name them freely)."""

    urls: list[str] = []
    for value in row.values():
        items = value if isinstance(value, list) else [value]
        urls.extend(item for item in items if _is_url(item))
    return urls


def _unique_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Issue rows repeat when a query joins several components or resolutions;
    # one line per issue keeps the first row's details.
    seen: set[str] = set()
    unique = []
    for row in rows:
        key = (
            f"issue:{row[ISSUE_NUMBER]}"
            if ISSUE_NUMBER in row
            else json.dumps(row, sort_keys=True, default=str)
        )
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def _issue_line(row: dict[str, Any]) -> str:
    text = next((row[field] for field in TEXT_FIELDS if row.get(field)), "")
    extras = [
        _count_phrase(column, value)
        for column, value in row.items()
        if column not in {ISSUE_NUMBER, *URL_FIELDS, *TEXT_FIELDS}
        and value not in (None, [], "")
        and not _is_url(value)
        and not (isinstance(value, list) and all(_is_url(item) for item in value))
    ]
    line = f"- **#{row[ISSUE_NUMBER]}** {text}".rstrip()
    return line + (f" ({', '.join(extras)})" if extras else "")


def _aggregate_line(row: dict[str, Any]) -> str:
    names = [_value(value) for value in row.values() if not _is_number(value)]
    counts = [
        _count_phrase(column, value) for column, value in row.items() if _is_number(value)
    ]
    return f"- {' / '.join(names) or 'Result'}: {', '.join(counts)}"


def _count_phrase(column: str, value: Any) -> str:
    if column.endswith("_count") and _is_number(value):
        noun = column[: -len("_count")].replace("_", " ")
        noun = {"discussion": "comment", "comments": "comment"}.get(noun, noun)
        return f"{_value(value)} {noun}{'' if value == 1 else 's'}"
    return f"{_label(column).lower()} {_value(value)}"


def _sort_value(row: dict[str, Any]) -> Any:
    return next((value for value in row.values() if _is_number(value)), None)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _label(column: str) -> str:
    return column.replace("_", " ").capitalize()


def _value(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)
