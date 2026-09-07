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
