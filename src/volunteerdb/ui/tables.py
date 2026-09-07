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


class SearchedTable(ui.table):
    """A table under a search box.

    `every` is the whole row set and `rows` (Quasar's) what the box lets
    through. So the widget, not a closure, holds what a section reloading
    in place replaces: `table.every = fresh_rows`, then the `apply` that
    wire_search returned, and the box's text, the sort and the page all
    survive the redraw (teams_page._roster_section)."""

    def __init__(
        self,
        *,
        columns: list[dict],
        rows: Rows,
        row_key: str = "id",
        pagination: int | dict | None = None,
    ) -> None:
        super().__init__(
            columns=columns, rows=rows, row_key=row_key, pagination=pagination
        )
        self.every: Rows = rows


def count_text(shown: int, total: int | None, noun: str) -> str:
    """'3 teams', '1 event', '3 of 12 teams'."""
    if total is None or shown == total:
        return f"{shown} {noun}{'s' if shown != 1 else ''}"
    return f"{shown} of {total} {noun}s"


def wire_search(
    search: ui.input,
    count: ui.label,
    table: SearchedTable,
    *,
    noun: str,
    compile: Callable[[Any], Any],
    text_filter: Callable[[Rows, str], Rows],
    query_filter: Callable[[Rows, Predicate], Rows] = lambda rows, pred: [
        r for r in rows if pred(r)
    ],
) -> Callable[[], None]:
    """Narrow `table` to the rows of `table.every` matching what is typed
    into `search`.

    `text_filter` answers plain text (lowercased); `compile` is the
    query_lang compiler for this table's fields, and `query_filter` applies
    its predicate -- the teams page overrides it to keep each hit's ancestors,
    so the tree indent stays honest. A filter that cannot run says so inline
    under the table, never as a toast: this runs on every keystroke.

    Returns `apply`, the same narrowing on demand: what a section that
    put a fresh row set into `table.every` calls next."""

    def apply() -> None:
        rows = table.every
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
    return apply
