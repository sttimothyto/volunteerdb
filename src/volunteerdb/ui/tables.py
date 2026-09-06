"""Search-as-you-type over a table whose rows are all already on the page.

The big listings (teams, events) hold the whole parish in `rows`, so a
search box narrows what the table shows without a query or a reload: plain
text is a substring match the page defines, and a SQL-shaped filter
(query_lang) is compiled to a predicate over the row dicts. The count label
under the table says how many of the rows are showing.
"""

from collections.abc import Callable
from typing import Any

from nicegui import ui

from .. import query_lang
from ..fp import Err

Rows = list[dict[str, Any]]
Predicate = Callable[[dict[str, Any]], bool]


def count_text(shown: int, total: int | None, noun: str) -> str:
    """'3 teams', '1 event', '3 of 12 teams'."""
    if total is None or shown == total:
        return f"{shown} {noun}{'s' if shown != 1 else ''}"
    return f"{shown} of {total} {noun}s"


def wire_search(
    search: ui.input,
    count: ui.label,
    table: ui.table,
    rows: Rows,
    *,
    noun: str,
    compile: Callable[[Any], Any],
    text_filter: Callable[[Rows, str], Rows],
    query_filter: Callable[[Rows, Predicate], Rows] = lambda rows, pred: [
        r for r in rows if pred(r)
    ],
) -> None:
    """Narrow `table` to the rows matching what is typed into `search`.

    `text_filter` answers plain text (lowercased); `compile` is the
    query_lang compiler for this table's fields, and `query_filter` applies
    its predicate -- the teams page overrides it to keep each hit's ancestors,
    so the tree indent stays honest. A filter that cannot run says so inline
    under the table, never as a toast: this runs on every keystroke."""

    def apply() -> None:
        text = (search.value or "").strip()
        ast = query_lang.parse(text) if text else None
        if ast is None:
            shown = rows if not text else text_filter(rows, text.lower())
        else:
            compiled = compile(ast)
            if isinstance(compiled, Err):
                count.set_text(f"query error: {compiled.error.message}")
                return
            shown = query_filter(rows, compiled.value)
        table.rows = shown
        table.update()  # assigning the property only stages it
        count.set_text(count_text(len(shown), len(rows), noun))

    search.on_value_change(apply)
