"""The small things every page draws the same way: the badges a role, a
phase, a workload band or an archived record wear, and the role dropdown's
options. One definition each, so a role reads the same on the roster, the
profile, the dashboard and the election page.
"""

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from nicegui import helpers, ui
from nicegui.elements.mixins.disableable_element import DisableableElement
from nicegui.events import ClickEventArguments

from ..models import ROLE_LABELS, ProposalStatus, TeamRole
from ..services import workload as workload_service
from ..services.elections import ProposalPhase

# What a role picker offers: the stored value to its display label.
ROLE_OPTIONS = {role.value: ROLE_LABELS[role] for role in TeamRole}


def empty_state(
    text: str,
    *,
    hint: str | None = None,
    action: str | None = None,
    href: str | None = None,
    on_click: Callable[..., Any] | None = None,
    marker: str = "empty-state",
) -> None:
    """A list with nothing in it says so, and offers the next thing.

    `text` is the fact ("Nobody matches “xyz”."), `hint` a quieter line
    under it, and `action` a button: a link when `href` is given, a
    handler otherwise. A blank page with a count of 0 leaves the reader
    guessing whether the search, the filter or the parish is empty."""
    with ui.column().classes("items-start gap-1 vdb-empty").mark(marker):
        ui.label(text).classes("text-gray-500")
        if hint:
            ui.label(hint).classes("text-sm text-gray-500")
        if action and href:
            ui.button(action).props(f'dense outline href="{href}"').mark("empty-action")
        elif action and on_click is not None:
            ui.button(action, on_click=on_click).props("dense outline").mark(
                "empty-action"
            )


def denied(reason: str, *, back: tuple[str, str], marker: str = "denied") -> None:
    """A refusal, or a page that is not there, with a way back.

    The sentence says whose page it is ("This event is visible to the
    members of its team."); the button goes to a page this reader can
    read. A refusal that ends in one sentence and no control leaves a
    member with the browser's back button and nothing else."""
    label, href = back
    with ui.column().classes("items-start gap-2 vdb-empty").mark(marker):
        ui.label(reason).classes("text-gray-500")
        ui.button(label, icon="arrow_back").props(f'dense outline href="{href}"').mark(
            "denied-back"
        )


def busy(
    handler: Callable[..., Any],
) -> Callable[[ClickEventArguments], Awaitable[None]]:
    """A click handler whose button says it is working.

    While `handler` runs the button that was clicked shows Quasar's spinner
    and takes no second click -- a sheet sync or a bulk invite is seconds
    of silence otherwise, and a second click is a second sync. Restored
    when the handler returns; a handler that reloads the page takes the
    button with it, and there is nothing to restore.

        ui.button("Sync now", on_click=busy(lambda: _sync_sheet(team_id)))

    `handler` is called the way NiceGUI would call it: with the event if it
    takes an argument, without one if not."""

    async def run(e: ClickEventArguments) -> None:
        button = e.sender
        button.props("loading")
        if isinstance(button, DisableableElement):
            button.disable()
        try:
            result = handler(e) if helpers.expects_arguments(handler) else handler()
            if helpers.should_await(result):
                await result
        finally:
            if not button.is_deleted:
                button.props(remove="loading")
                if isinstance(button, DisableableElement):
                    button.enable()

    return run


def role_badge(role: TeamRole) -> ui.badge:
    return ui.badge(ROLE_LABELS[role])


def inactive_badge() -> ui.badge:
    """An archived volunteer or a wound-down team."""
    return ui.badge("inactive", color="muted")


def workload_badge(
    score: Decimal,
    band: workload_service.Band,
    *,
    prefix: str = "",
    tooltip: str = "Workload score: team weights × role multipliers, all ministries",
) -> ui.badge:
    """A band in its own colour, with whichever label reads on it (the
    contrast rule the admin page enforces when the colour is chosen)."""
    return (
        ui.badge(f"{prefix}{band.label} · {float(score):g}")
        .style(
            f"background-color: {band.color}; "
            f"color: {workload_service.text_colour(band.color)}"
        )
        .tooltip(tooltip)
    )


def phase_badge(proposal, phase: ProposalPhase | None) -> None:
    """Where a proposal stands: its phase while open, its outcome once
    decided."""
    if phase is ProposalPhase.nominating:
        ui.badge(f"Nominating until {proposal.nomination_deadline}", color="primary")
    elif phase is ProposalPhase.voting:
        ui.badge(f"Voting until {proposal.voting_deadline}", color="warning")
    elif phase is ProposalPhase.concluded:
        ui.badge("Awaiting decision", color="purple")
    elif proposal.status == ProposalStatus.appointed.value:
        ui.badge("Appointed", color="positive")
    else:
        ui.badge("Cancelled", color="muted")
