"""The small things every page draws the same way: the badges a role, a
phase, a workload band or an archived record wear, and the role dropdown's
options. One definition each, so a role reads the same on the roster, the
profile, the dashboard and the election page.
"""

from decimal import Decimal

from nicegui import ui

from ..models import ROLE_LABELS, ProposalStatus, TeamRole
from ..services import workload as workload_service
from ..services.elections import ProposalPhase

# What a role picker offers: the stored value to its display label.
ROLE_OPTIONS = {role.value: ROLE_LABELS[role] for role in TeamRole}


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
