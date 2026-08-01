"""Planning: vacancies (teams missing leadership) + collaborative proposals.

Admins see every team; leaders/seconds see their managed subtree. Anyone who
can see a vacancy can propose a volunteer for it; accepting a proposal — which
creates the membership — needs can_manage_team for that team, re-checked in
the action handler.
"""

from nicegui import ui

from ..models import PROPOSAL_STATUS_LABELS, ROLE_LABELS, ProposalStatus, TeamRole
from ..permissions import require, team_ids_map
from ..services import planning as planning_service
from ..services import volunteers as volunteer_service
from ..services import workload as workload_service
from .context import action_session, notify_errors, page_session
from .layout import frame

ROLE_OPTIONS = {role.value: ROLE_LABELS[role] for role in TeamRole}

_STATUS_COLORS = {
    ProposalStatus.accepted.value: "positive",
    ProposalStatus.declined.value: "negative",
    ProposalStatus.withdrawn.value: "grey",
}


@ui.page("/planning")
async def planning_page():
    async with page_session() as (session, actor):
        allowed = actor.is_admin or bool(actor.managed_team_ids)
        if not allowed:
            with frame("Planning", actor):
                ui.label(
                    "Planning is available to admins and to team leaders/seconds."
                ).classes("text-gray-500")
            return
        vacancy_rows = await planning_service.vacancies(session, actor)
        proposals = await planning_service.list_proposals(session, actor)
        volunteer_options = {
            v.id: v.full_name for v in await volunteer_service.search(session)
        }
        team_sets = await team_ids_map(
            session, [pv.proposal.volunteer_id for pv in proposals]
        )
        wl = await workload_service.visible_scores(session, actor, team_sets)

    open_by_team: dict[int, list] = {}
    resolved = []
    for pv in proposals:
        if pv.proposal.status == ProposalStatus.proposed.value:
            open_by_team.setdefault(pv.proposal.team_id, []).append(pv)
        else:
            resolved.append(pv)
    vacant_team_ids = {r.team.id for r in vacancy_rows}

    def proposal_row(pv) -> None:
        p = pv.proposal
        with ui.row().classes("w-full items-center gap-2 p-2 rounded bg-gray-50"):
            ui.link(pv.volunteer.full_name, f"/volunteers/{pv.volunteer.id}").classes(
                "font-medium"
            )
            ui.badge(ROLE_LABELS[TeamRole(p.role)])
            if p.volunteer_id in wl:
                score, band = wl[p.volunteer_id]
                ui.badge(f"{band.label} · {float(score):g}").style(
                    f"background-color: {band.color}"
                ).tooltip("Current workload")
            if p.note:
                ui.label(p.note).classes("text-sm text-gray-600")
            ui.space()
            proposer = pv.proposer_email or "a deleted account"
            ui.label(f"proposed by {proposer}").classes("text-xs text-gray-400")
            if p.status == ProposalStatus.proposed.value:
                if actor.can_manage_team(p.team_id):
                    ui.button(
                        "Accept",
                        icon="check",
                        on_click=notify_errors(lambda _, pid=p.id: _accept(pid)),
                    ).props("dense color=positive")
                    ui.button(
                        "Decline",
                        on_click=notify_errors(lambda _, pid=p.id: _decline(pid)),
                    ).props("dense flat color=negative")
                if p.proposed_by == actor.user.id:
                    ui.button(
                        "Withdraw",
                        on_click=notify_errors(lambda _, pid=p.id: _withdraw(pid)),
                    ).props("dense flat")
            else:
                ui.badge(
                    PROPOSAL_STATUS_LABELS[ProposalStatus(p.status)],
                    color=_STATUS_COLORS.get(p.status, "grey"),
                )
                if pv.decider_email:
                    ui.label(f"by {pv.decider_email}").classes("text-xs text-gray-400")

    def propose_form(team_id: int, default_role: TeamRole) -> None:
        with ui.row().classes("w-full items-center gap-2"):
            who = (
                ui.select(volunteer_options, label="Volunteer", with_input=True)
                .props("outlined dense")
                .classes("w-64")
            )
            role = (
                ui.select(ROLE_OPTIONS, label="Role", value=default_role.value)
                .props("outlined dense")
                .classes("w-52")
            )
            note = (
                ui.input("Why them? (optional)").props("outlined dense").classes("grow")
            )

            @notify_errors
            async def submit() -> None:
                if not who.value:
                    ui.notify("Pick a volunteer", color="warning")
                    return
                async with action_session() as (session, actor):
                    require(actor.can_manage_team(team_id), "propose for this team")
                    await planning_service.propose(
                        session,
                        team_id=team_id,
                        volunteer_id=who.value,
                        role=TeamRole(role.value),
                        proposed_by=actor.user.id,
                        note=note.value,
                    )
                ui.navigate.reload()

            ui.button("Propose", icon="person_add", on_click=submit).props("dense")

    with frame("Planning", actor):
        ui.label("Vacancies").classes("text-lg font-medium")
        if not vacancy_rows:
            ui.label("Every team has a leader and a second-in-command. 🎉").classes(
                "text-positive"
            )
        for r in vacancy_rows:
            with ui.card().classes("w-full gap-2 p-3"):
                with ui.row().classes("w-full items-center gap-2"):
                    ui.link(r.path, f"/teams/{r.team.id}").classes("font-medium")
                    if r.missing_leader:
                        ui.badge("no leader", color="negative")
                    if r.missing_second:
                        ui.badge("no second-in-command", color="warning")
                    ui.space()
                    ui.label(f"{r.total} member{'s' if r.total != 1 else ''}").classes(
                        "text-sm text-gray-600"
                    )
                for pv in open_by_team.get(r.team.id, []):
                    proposal_row(pv)
                propose_form(
                    r.team.id, TeamRole.leader if r.missing_leader else TeamRole.second
                )

        stray = [
            pv
            for team_id, pvs in open_by_team.items()
            if team_id not in vacant_team_ids
            for pv in pvs
        ]
        if stray:
            ui.label("Other open proposals").classes("text-lg font-medium mt-4")
            ui.label("The role was filled after these were proposed.").classes(
                "text-sm text-gray-500"
            )
            with ui.column().classes("w-full gap-1"):
                for pv in stray:
                    with ui.column().classes("w-full gap-1"):
                        ui.label(pv.path).classes("text-sm font-medium")
                        proposal_row(pv)

        if resolved:
            ui.label("Recently decided").classes("text-lg font-medium mt-4")
            with ui.column().classes("w-full gap-1"):
                for pv in resolved[:20]:
                    with ui.column().classes("w-full gap-1"):
                        ui.label(pv.path).classes("text-sm font-medium")
                        proposal_row(pv)


async def _accept(proposal_id: int) -> None:
    async with action_session() as (session, actor):
        proposal = await planning_service.get(session, proposal_id)
        if proposal is None:
            raise LookupError("proposal vanished")
        require(
            actor.can_manage_team(proposal.team_id), "decide proposals for this team"
        )
        await planning_service.accept(session, proposal_id, decided_by=actor.user.id)
    ui.navigate.reload()


async def _decline(proposal_id: int) -> None:
    async with action_session() as (session, actor):
        proposal = await planning_service.get(session, proposal_id)
        if proposal is None:
            raise LookupError("proposal vanished")
        require(
            actor.can_manage_team(proposal.team_id), "decide proposals for this team"
        )
        await planning_service.decline(session, proposal_id, decided_by=actor.user.id)
    ui.navigate.reload()


async def _withdraw(proposal_id: int) -> None:
    async with action_session() as (session, actor):
        proposal = await planning_service.get(session, proposal_id)
        if proposal is None:
            raise LookupError("proposal vanished")
        require(
            proposal.proposed_by == actor.user.id
            or actor.can_manage_team(proposal.team_id),
            "withdraw this proposal",
        )
        await planning_service.withdraw(session, proposal_id, decided_by=actor.user.id)
    ui.navigate.reload()
