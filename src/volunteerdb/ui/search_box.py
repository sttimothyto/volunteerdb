"""Search input with a live suggestion dropdown, shared by the dashboard and
the volunteers list.

Typing (debounced client-side, ≥ SUGGEST_MIN_CHARS) fills a QMenu anchored under
the input with matching teams and volunteers. Clicking a volunteer opens the
side panel, a team opens its page, and a trailing row falls back to the full
result list — which is also what Enter and the Search button still do.

The menu's children are rebuilt on every lookup rather than pre-rendered and
toggled: NiceGUI's element filters ignore a QMenu's open/closed state, so a
populated-but-closed menu would still count as visible to the UI tests.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from nicegui import ui

from ..services import teams as team_service
from ..services import volunteers as volunteer_service
from .context import action_session, notify_errors

SUGGEST_MIN_CHARS = 2
SUGGEST_LIMIT = 6  # per category
SUGGEST_DEBOUNCE_MS = 250


def search_box(
    label: str,
    *,
    on_submit: Callable[[str], Any],
    on_pick_volunteer: Callable[[int], Any],
    value: str = "",
    at: datetime | None = None,
    as_of: str = "",
) -> ui.input:
    """Build the input + Search button (and the suggestion menu) in the current
    row, and return the input so callers can read its value.

    `on_submit` receives the current text on Enter, the Search button, and the
    "see all results" row; `on_pick_volunteer` receives the volunteer id of a
    clicked suggestion (normally ``VolunteerPanel.open``).
    """
    asof_query = f"?as_of={as_of}" if as_of else ""
    search = (
        ui.input(label, value=value)
        .props(f"outlined dense clearable debounce={SUGGEST_DEBOUNCE_MS}")
        .classes("w-72")
    )
    with search:
        menu = (
            ui.menu()
            .props("no-parent-event no-focus no-refocus fit square max-height=20rem")
            .classes("vdb-suggest")
            .mark("suggest-menu")
        )

    latest = 0  # only the newest in-flight lookup may render

    @notify_errors
    async def suggest() -> None:
        nonlocal latest
        latest += 1
        token = latest
        text = (search.value or "").strip()
        if len(text) < SUGGEST_MIN_CHARS:
            menu.close()
            menu.clear()
            return

        async with action_session() as (session, actor):
            found = await volunteer_service.search(
                session,
                text,
                at=at,
                include_inactive=actor.is_admin,
                actor=actor,
                limit=SUGGEST_LIMIT,
            )
            team_hits = (await team_service.search(session, text, at=at))[
                :SUGGEST_LIMIT
            ]
        if token != latest:
            return  # a later keystroke is already on its way

        menu.clear()
        with menu:
            if team_hits:
                ui.item_label("Teams").props("header")
            for team, path in team_hits:
                ui.menu_item(
                    path,
                    on_click=lambda tid=team.id: ui.navigate.to(
                        f"/teams/{tid}{asof_query}"
                    ),
                ).mark(f"suggest-team-{team.id}")
            if found:
                ui.item_label("Volunteers").props("header")
            for volunteer in found:
                with ui.menu_item(
                    on_click=lambda vid=volunteer.id: on_pick_volunteer(vid)
                ).mark(f"suggest-volunteer-{volunteer.id}"):
                    ui.item_section(volunteer.full_name)
                    if not volunteer.is_active:
                        with ui.item_section().props("side"):
                            ui.badge("inactive", color="grey")
            if not team_hits and not found:
                ui.item("Nothing found")
            else:
                ui.separator()
                ui.menu_item(
                    f"See every match for “{text}”",
                    on_click=lambda t=text: on_submit(t),
                )
        menu.open()

    def reopen() -> None:
        """Escape and outside clicks close the dropdown; going back to a box
        that still holds a query brings the last suggestions back, since
        retyping the same text is not a value change and would not.

        Both events are needed: dismissing with Escape leaves the caret where
        it was, so clicking the box again fires no focus event."""
        if (
            menu.default_slot.children
            and len((search.value or "").strip()) >= SUGGEST_MIN_CHARS
        ):
            menu.open()

    search.on_value_change(suggest)
    search.on("focus", reopen)
    search.on("click", reopen)
    search.on("keydown.enter", lambda: on_submit(search.value or ""))
    search.on("keydown.esc", menu.close)
    ui.button("Search", on_click=lambda: on_submit(search.value or "")).props("dense")
    return search
