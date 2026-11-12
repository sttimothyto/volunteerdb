"""Putting somebody on a team and taking them off it.

A roster is edited from both ends: the team page adds a volunteer to this
team and the volunteer page adds this volunteer to a team, and either can
take a name off again. It is one membership and one service call whichever
page it starts on, so the row, the handlers and the question asked before a
removal live here, where the two pages cannot come to word them differently.
"""

from collections.abc import Awaitable, Callable

from nicegui import ui

from ..models import TeamRole
from ..services import memberships as membership_service
from .a11y import heading
from .context import PageCtx, Refresh, run_command
from .forms import confirm, required, valid
from .widgets import ROLE_OPTIONS


def add_row(
    title: str,
    options: dict[int, str],
    *,
    label: str,
    icon: str,
    add: Callable[[int | None, str], Awaitable[None]],
    marker: str | None = None,
) -> ui.select:
    """A picker, a role and an Add button under a heading. `options` is what
    the picker offers (volunteers, or teams) and `add` is told the id picked
    and the role's value. The picker is returned so an empty roster's button
    can put the cursor in it."""
    heading(title, level=2)
    with ui.row().classes("items-center gap-2"):
        pick = (
            required(ui.select(options, label=label, with_input=True))
            .props("outlined dense")
            .classes("w-64")
        )
        role = (
            ui.select(ROLE_OPTIONS, label="Role", value=TeamRole.member.value)
            .props("outlined dense")
            .classes("w-52")
        )
        button = ui.button(
            "Add",
            icon=icon,
            on_click=lambda: add(pick.value, role.value) if valid(pick) else None,
        ).props("dense")
        if marker is not None:
            button.mark(marker)
    return pick


async def add(
    volunteer_id: int | None, team_id: int | None, role_value: str, *, success: str
) -> None:
    if not (volunteer_id and team_id):
        return  # the picker's own rule said so already (forms.valid)

    async def command(ctx: PageCtx):
        return await membership_service.assign(
            ctx.session, ctx.actor, volunteer_id, team_id, TeamRole(role_value)
        )

    await run_command(command, reload=True, success=success)


async def remove(
    membership_id: int, name: str, team: str, refresh: Refresh | None = None
) -> None:
    """Take somebody off a roster, once the leader has said so twice: the
    icon is small, the row is one of sixty, and the wrong one is a phone
    call to make. `refresh` redraws the roster in place; without one the
    page reloads."""
    if not await confirm(
        f"Remove {name} from the {team} roster?",
        detail=(
            "They stay in the parish list and on their other teams. "
            "The history keeps the membership."
        ),
        yes=f"Remove {name} from {team}",
        danger=True,
    ):
        return
    await run_command(
        lambda ctx: membership_service.remove(ctx.session, ctx.actor, membership_id),
        refresh=refresh,
        success=f"Removed from {team}",
    )
