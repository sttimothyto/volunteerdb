"""Search input with a live suggestion dropdown, shared by the dashboard and
the volunteers list.

Typing (debounced client-side, ≥ SUGGEST_MIN_CHARS) fills a QMenu anchored under
the input with matching teams and volunteers. Clicking a volunteer opens the
side panel, a team opens its page, and a trailing row falls back to the full
result list — which is also what Enter and the Search button still do.

The menu's children are rebuilt on every lookup rather than pre-rendered and
toggled: NiceGUI's element filters ignore a QMenu's open/closed state, so a
populated-but-closed menu would still count as visible to the UI tests.

The box is a combobox (WAI-ARIA): role, aria-expanded, aria-controls to the
listbox, and an aria-activedescendant the arrow keys move through the
options; Enter opens the marked one, or runs the search when none is. The
highlight is the input's own attribute and the options' class -- widgets,
not a cell -- and each option carries what it does, so the keyboard's pick
runs the same thing as a click.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from nicegui import ui

from .. import query_lang
from ..services import teams as team_service
from ..services import volunteers as volunteer_service
from .context import page_ctx
from .widgets import inactive_badge

SUGGEST_MIN_CHARS = 2
SUGGEST_LIMIT = 6  # per category
SUGGEST_DEBOUNCE_MS = 250

ACTIVE = "vdb-active"  # the option the keyboard is on (theme.css)


class Option(ui.menu_item):
    """One suggestion, with what Enter or a click does kept on it. A team is
    a real link (`href`), so right-click and a new tab work; the others run
    their `activate` on click."""

    def __init__(
        self, text: str = "", *, activate: Callable[[], Any], href: str = ""
    ) -> None:
        super().__init__(text, on_click=None if href else lambda: activate())
        self.activate = activate
        self.props('role="option" aria-selected="false"')
        if href:
            self.props(f'href="{href}"')


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
    # grow, not a fixed width: the box takes whatever the row has left, so it
    # stretches with the window. Callers must not put a ui.space() in the same
    # row — QSpace grows too, and two growing siblings split the slack.
    search = (
        ui.input(label, value=value)
        .props(f"outlined dense clearable debounce={SUGGEST_DEBOUNCE_MS}")
        # QInput hands attributes that are not its props to the native input
        .props('role="combobox" aria-autocomplete="list" aria-haspopup="listbox"')
        .props('aria-expanded="false"')
        .classes("grow")
    )
    with search:
        menu = (
            ui.menu()
            .props("no-parent-event no-focus no-refocus fit square max-height=20rem")
            .classes("vdb-suggest")
            .mark("suggest-menu")
        )
    menu.on_value_change(
        lambda e: search.props(f'aria-expanded="{"true" if e.value else "false"}"')
    )

    def options() -> list[Option]:
        return [o for o in menu.descendants() if isinstance(o, Option)]

    def highlight(step: int) -> None:
        """Arrow keys: the mark moves one option, wrapping at either end, and
        the input's aria-activedescendant names where it is."""
        opts = options()
        if not opts:
            return
        ids = [f"c{o.id}" for o in opts]
        current = search.props.get("aria-activedescendant")
        index = ids.index(current) + step if current in ids else (0 if step > 0 else -1)
        chosen = ids[index % len(ids)]
        for option, oid in zip(opts, ids, strict=True):
            on = oid == chosen
            option.classes(add=ACTIVE if on else "", remove="" if on else ACTIVE)
            option.props(f'aria-selected="{"true" if on else "false"}"')
        search.props(f'aria-activedescendant="{chosen}"')
        menu.open()

    def pick_or_submit() -> Any:
        """Enter: the marked option, or the search when none is marked."""
        current = search.props.get("aria-activedescendant")
        for option in options():
            if f"c{option.id}" == current:
                menu.close()
                return option.activate()
        return on_submit(search.value or "")

    async def suggest() -> None:
        text = (search.value or "").strip()
        if len(text) < SUGGEST_MIN_CHARS:
            menu.close()
            menu.clear()
            return

        if query_lang.parse(text) is not None:
            # a WHERE filter, not a name: offer to run it instead of
            # substring-suggesting against the raw SQL text
            with listbox():
                Option(f"Run query: {text}", activate=lambda t=text: on_submit(t)).mark(
                    "suggest-query"
                )
                ui.menu_item("Query syntax help").props(
                    'href="/manual/reference/query-language.html" target="_blank"'
                )
            menu.open()
            return

        async with page_ctx() as ctx:
            found = await volunteer_service.search(
                ctx.session,
                text,
                at=at,
                include_inactive=ctx.actor.is_admin,
                actor=ctx.actor,
                limit=SUGGEST_LIMIT,
            )
            team_hits = (await team_service.search(ctx.session, text, at=at))[
                :SUGGEST_LIMIT
            ]
        if (search.value or "").strip() != text:
            return  # a later keystroke is already on its way; the box is the state

        with listbox():
            if team_hits:
                ui.item_label("Teams").props("header")
            for team, path in team_hits:
                href = f"/teams/{team.id}{asof_query}"
                Option(path, href=href, activate=lambda h=href: ui.navigate.to(h)).mark(
                    f"suggest-team-{team.id}"
                )
            if found:
                ui.item_label("Volunteers").props("header")
            for volunteer in found:
                with Option(
                    activate=lambda vid=volunteer.id: on_pick_volunteer(vid)
                ).mark(f"suggest-volunteer-{volunteer.id}"):
                    ui.item_section(volunteer.full_name)
                    if not volunteer.is_active:
                        with ui.item_section().props("side"):
                            inactive_badge()
            if not team_hits and not found:
                ui.item("Nothing found")
            else:
                ui.separator()
                Option(
                    f"See every match for “{text}”",
                    activate=lambda t=text: on_submit(t),
                )
        menu.open()

    def listbox() -> ui.element:
        """A fresh list for a fresh lookup: the old options go, the mark
        with them, and the input points at the new list."""
        menu.clear()
        search.props(remove="aria-activedescendant")
        with menu:
            box = ui.element("div").props('role="listbox"')
        search.props(f'aria-controls="c{box.id}"')
        return box

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
    # .prevent: the caret must not jump to the ends of the text
    search.on("keydown.down.prevent", lambda: highlight(+1))
    search.on("keydown.up.prevent", lambda: highlight(-1))
    search.on("keydown.enter", pick_or_submit)
    search.on("keydown.esc", menu.close)
    ui.button("Search", on_click=lambda: on_submit(search.value or "")).props("dense")
    return search
