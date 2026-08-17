"""Planning: vacancies + the nomination and STAR-voting pipeline.

/planning lists vacancies (managers can open a proposal from one) and the
proposals the actor may see; /planning/{id} is one proposal's workroom:
candidates with their current commitments (the overwork check), the voting
roll with turnout, the ballot form during the voting phase, and the tally
plus appoint/new-round actions once voting has concluded. Permission gates
mirror the API: managers run the seat, voting members nominate and vote,
and every action handler re-checks inside its own action_session.
"""

from datetime import date, timedelta

from nicegui import ui

from ..models import ROLE_LABELS, ProposalStatus, TeamRole
from ..permissions import require, team_ids_map
from ..services import planning as planning_service
from ..services import volunteers as volunteer_service
from ..services import workload as workload_service
from ..star import StarResult
from .context import action_session, notify_errors, page_session
from .layout import frame

ROLE_OPTIONS = {role.value: ROLE_LABELS[role] for role in TeamRole}

IGNATIAN_NOTE = (
    "Ignatian election: 1. pray separately, 2. vote separately, "
    "then 3. debate together — and repeat as needed. "
    "This is a consulatative vote; the final appointment is an act proper to the pastor of the parish. See Code of Canon Law 536 §1, 515 §1."
)
STAR_NOTE = (
    "STAR voting: score every candidate 0 (worst) – 5 (best) on their own merits. Unlike "
    "first-past-the-post, similar candidates don't split the vote — there "
    "is no spoiler effect, so score honestly."
    "Individual votes are secret. Final points will be visible."
)

Phase = planning_service.ProposalPhase


def phase_badge(proposal, phase: Phase | None) -> None:
    if phase is Phase.nominating:
        ui.badge(f"Nominating until {proposal.nomination_deadline}", color="primary")
    elif phase is Phase.voting:
        ui.badge(f"Voting until {proposal.voting_deadline}", color="orange")
    elif phase is Phase.concluded:
        ui.badge("Awaiting decision", color="purple")
    elif proposal.status == ProposalStatus.appointed.value:
        ui.badge("Appointed", color="positive")
    else:
        ui.badge("Cancelled", color="grey")


def _deadline_inputs(d1_default: date, d2_default: date) -> tuple[ui.input, ui.input]:
    d1 = (
        ui.input("Nominations close (YYYY-MM-DD)", value=str(d1_default))
        .props("outlined dense")
        .classes("w-full")
    )
    d2 = (
        ui.input("Voting closes (YYYY-MM-DD)", value=str(d2_default))
        .props("outlined dense")
        .classes("w-full")
    )
    return d1, d2


def _parse_deadlines(d1: ui.input, d2: ui.input) -> tuple[date, date] | None:
    try:
        return date.fromisoformat(d1.value or ""), date.fromisoformat(d2.value or "")
    except ValueError:
        ui.notify("Deadlines must be YYYY-MM-DD dates", color="warning")
        return None


@ui.page("/planning")
async def planning_page():
    async with page_session() as (session, actor):
        if not actor.can_access_planning:
            with frame("Planning", actor):
                ui.label(
                    "Planning is available to admins, team leaders/seconds, "
                    "and the voting members of a proposal."
                ).classes("text-gray-500")
            return
        can_create = actor.is_admin or bool(actor.managed_team_ids)
        vacancy_rows = (
            await planning_service.vacancies(session, actor) if can_create else []
        )
        summaries = await planning_service.list_proposals(session, actor)
        volunteer_options = (
            await volunteer_service.name_map(session) if can_create else {}
        )

    open_rows = [s for s in summaries if s.proposal.status == ProposalStatus.open.value]
    decided_rows = [
        s for s in summaries if s.proposal.status != ProposalStatus.open.value
    ]
    proposal_team_ids = {s.proposal.team_id for s in open_rows}

    def summary_row(s: planning_service.ProposalSummary) -> None:
        p = s.proposal
        with ui.row().classes("w-full items-center gap-2 p-2 rounded bg-gray-50"):
            ui.link(
                f"{s.path}: {ROLE_LABELS[TeamRole(p.role)]}", f"/planning/{p.id}"
            ).classes("font-medium")
            phase_badge(p, s.phase)
            ui.space()
            ui.label(
                f"{s.candidate_count} candidate{'s' if s.candidate_count != 1 else ''}"
                f" · {s.voted_count}/{s.voter_count} ballots"
            ).classes("text-sm text-gray-600")

    def create_dialog(team_id: int, path: str, default_role: TeamRole) -> None:
        with ui.dialog() as dialog, ui.card().classes("w-[28rem] gap-3"):
            ui.label(f"Propose for {path}").classes("text-lg font-medium")
            role = (
                ui.select(ROLE_OPTIONS, label="Role", value=default_role.value)
                .props("outlined dense")
                .classes("w-full")
            )
            who = (
                ui.select(volunteer_options, label="First candidate", with_input=True)
                .props("outlined dense")
                .classes("w-full")
            )
            why = ui.input("Why them?").props("outlined dense").classes("w-full")
            today = planning_service.local_today()
            d1, d2 = _deadline_inputs(
                today + timedelta(days=14), today + timedelta(days=28)
            )
            ui.label(
                "The voting roll is prefilled: this team's leader, second and "
                "core members, plus the clergy team. Voting members may add "
                "candidates until nominations close."
            ).classes("text-xs text-gray-500")

            @notify_errors
            async def save() -> None:
                if not who.value:
                    ui.notify("Pick the first candidate", color="warning")
                    return
                if (deadlines := _parse_deadlines(d1, d2)) is None:
                    return
                async with action_session() as (session, actor):
                    require(
                        actor.can_manage_team(team_id), "open proposals for this team"
                    )
                    proposal = await planning_service.create_proposal(
                        session,
                        team_id=team_id,
                        role=TeamRole(role.value),
                        nomination_deadline=deadlines[0],
                        voting_deadline=deadlines[1],
                        created_by=actor.user.id,
                        candidates=[
                            planning_service.CandidateInput(who.value, why.value)
                        ],
                    )
                dialog.close()
                ui.navigate.to(f"/planning/{proposal.id}")

            with ui.row().classes("justify-end w-full gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                # not "Open proposal": that is a substring of the section
                # header "Open proposals" and would confuse content matching
                ui.button("Create proposal", icon="how_to_vote", on_click=save)
        dialog.open()

    with frame("Planning", actor):
        if can_create:
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
                        if r.team.id in proposal_team_ids:
                            ui.badge("proposal open", color="primary")
                        ui.space()
                        ui.label(
                            f"{r.total} member{'s' if r.total != 1 else ''}"
                        ).classes("text-sm text-gray-600")
                        ui.button(
                            "Start proposal",
                            icon="how_to_vote",
                            on_click=lambda _, tid=r.team.id, path=r.path, role=(TeamRole.leader if r.missing_leader else TeamRole.second): (
                                create_dialog(tid, path, role)
                            ),
                        ).props("dense outline")

        if open_rows:
            ui.label("Open proposals").classes("text-lg font-medium mt-4")
            with ui.column().classes("w-full gap-1"):
                for s in open_rows:
                    summary_row(s)
        elif not can_create:
            ui.label("No open proposals need you right now.").classes("text-gray-500")

        if decided_rows:
            ui.label("Recently decided").classes("text-lg font-medium mt-4")
            with ui.column().classes("w-full gap-1"):
                for s in decided_rows[:20]:
                    summary_row(s)


@ui.page("/planning/{proposal_id}")
async def proposal_detail(proposal_id: int):
    async with page_session() as (session, actor):
        try:
            view = await planning_service.detail(session, proposal_id)
        except LookupError:
            with frame("Proposal not found", actor):
                ui.label(f"No proposal with id {proposal_id}.")
            return
        if not actor.can_view_proposal(proposal_id, view.proposal.team_id):
            with frame("Planning", actor):
                ui.label(
                    "This proposal is visible to its voting members and to the "
                    "team's managers."
                ).classes("text-gray-500")
            return
        can_manage = actor.can_manage_team(view.proposal.team_id)
        is_voter = (
            actor.volunteer_id is not None and proposal_id in actor.voter_proposal_ids
        )
        my = (
            await planning_service.my_scores(session, proposal_id, actor.volunteer_id)
            if is_voter
            else {}
        )
        team_sets = await team_ids_map(
            session, [cv.volunteer.id for cv in view.candidates]
        )
        wl = await workload_service.visible_scores(session, actor, team_sets)
        volunteer_options = (
            await volunteer_service.name_map(session) if can_manage or is_voter else {}
        )

    p = view.proposal
    phase = view.phase
    nominating = phase is Phase.nominating
    voting = phase is Phase.voting
    names = {cv.candidate.id: cv.volunteer.full_name for cv in view.candidates}

    @notify_errors
    async def _managed_action(what: str, action) -> None:
        async with action_session() as (session, actor):
            current = await planning_service.get(session, proposal_id)
            if current is None:
                raise LookupError("proposal vanished")
            require(actor.can_manage_team(current.team_id), what)
            await action(session, actor)
        ui.navigate.reload()

    def edit_deadlines_dialog() -> None:
        with ui.dialog() as dialog, ui.card().classes("w-96 gap-3"):
            ui.label("Edit deadlines").classes("text-lg font-medium")
            d1, d2 = _deadline_inputs(p.nomination_deadline, p.voting_deadline)

            async def save() -> None:
                if (deadlines := _parse_deadlines(d1, d2)) is None:
                    return
                dialog.close()
                await _managed_action(
                    "manage proposals for this team",
                    lambda session, actor: planning_service.update_proposal(
                        session,
                        proposal_id,
                        nomination_deadline=deadlines[0],
                        voting_deadline=deadlines[1],
                    ),
                )

            with ui.row().classes("justify-end w-full gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Save", on_click=save)
        dialog.open()

    async def cancel_proposal() -> None:
        with ui.dialog() as confirm, ui.card().classes("w-96 gap-3"):
            ui.label("Cancel this proposal? Ballots are discarded with it.")
            with ui.row().classes("justify-end w-full gap-2"):
                ui.button("Keep it", on_click=lambda: confirm.submit(False)).props(
                    "flat"
                )
                ui.button(
                    "Yes, cancel it", on_click=lambda: confirm.submit(True)
                ).props("color=negative")
        if not await confirm:
            return
        await _managed_action(
            "manage proposals for this team",
            lambda session, actor: planning_service.cancel(
                session, proposal_id, decided_by=actor.user.id
            ),
        )

    async def appoint(candidate_id: int) -> None:
        with ui.dialog() as confirm, ui.card().classes("w-96 gap-3"):
            ui.label(
                f"Appoint {names[candidate_id]} as "
                f"{ROLE_LABELS[TeamRole(p.role)]}? This assigns the role "
                "immediately."
            )
            with ui.row().classes("justify-end w-full gap-2"):
                ui.button("Back", on_click=lambda: confirm.submit(False)).props("flat")
                ui.button("Yes, appoint", on_click=lambda: confirm.submit(True))
        if not await confirm:
            return
        await _managed_action(
            "appoint for this team",
            lambda session, actor: planning_service.appoint(
                session, proposal_id, candidate_id, decided_by=actor.user.id
            ),
        )

    def new_round_dialog() -> None:
        with ui.dialog() as dialog, ui.card().classes("w-96 gap-3"):
            ui.label("Start a new round").classes("text-lg font-medium")
            ui.label(
                "Candidates and the voting roll carry over; ballots do not."
            ).classes("text-sm text-gray-500")
            today = planning_service.local_today()
            d1, d2 = _deadline_inputs(
                today + timedelta(days=14), today + timedelta(days=28)
            )

            @notify_errors
            async def save() -> None:
                if (deadlines := _parse_deadlines(d1, d2)) is None:
                    return
                async with action_session() as (session, actor):
                    current = await planning_service.get(session, proposal_id)
                    if current is None:
                        raise LookupError("proposal vanished")
                    require(
                        actor.can_manage_team(current.team_id),
                        "manage proposals for this team",
                    )
                    fresh = await planning_service.new_round(
                        session,
                        proposal_id,
                        created_by=actor.user.id,
                        nomination_deadline=deadlines[0],
                        voting_deadline=deadlines[1],
                    )
                dialog.close()
                ui.navigate.to(f"/planning/{fresh.id}")

            with ui.row().classes("justify-end w-full gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Start round", icon="restart_alt", on_click=save)
        dialog.open()

    with frame(f"{view.path}: {ROLE_LABELS[TeamRole(p.role)]}", actor):
        with ui.row().classes("w-full items-center gap-2"):
            ui.link(view.path, f"/teams/{p.team_id}").classes("font-medium")
            ui.badge(ROLE_LABELS[TeamRole(p.role)])
            phase_badge(p, phase)
            ui.space()
            if can_manage and p.status == ProposalStatus.open.value:
                ui.button(
                    "Edit deadlines",
                    icon="edit_calendar",
                    on_click=edit_deadlines_dialog,
                ).props("dense outline")
                ui.button("Cancel proposal", on_click=cancel_proposal).props(
                    "dense outline color=negative"
                )
        with ui.row().classes("w-full gap-4 text-sm text-gray-600"):
            ui.label(f"Nominations close {p.nomination_deadline}")
            ui.label(f"Voting closes {p.voting_deadline}")
            if view.creator_email:
                ui.label(f"opened by {view.creator_email}")
            if p.decided_at is not None and view.decider_email:
                ui.label(f"decided by {view.decider_email}")
        if p.notes:
            ui.label(p.notes).classes("text-gray-600")

        with ui.card().classes("w-full p-3"):
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.icon("campaign", size="sm").classes("text-primary")
                ui.label(IGNATIAN_NOTE).classes("text-sm italic")

        ui.label("Candidates").classes("text-lg font-medium mt-2")
        for cv in view.candidates:
            cid = cv.candidate.id
            with ui.card().classes("w-full gap-2 p-3"):
                with ui.row().classes("w-full items-center gap-2"):
                    ui.link(
                        cv.volunteer.full_name, f"/volunteers/{cv.volunteer.id}"
                    ).classes("font-medium")
                    if p.appointed_candidate_id == cid:
                        ui.badge("Appointed", color="positive")
                    if view.tally and view.tally.winner_id == cid:
                        ui.badge("STAR winner", color="primary")
                    if cv.volunteer.id in wl:
                        score, band = wl[cv.volunteer.id]
                        ui.badge(f"{band.label} · {float(score):g}").style(
                            f"background-color: {band.color}"
                        ).tooltip("Current workload")
                    ui.space()
                    if cv.nominator_email:
                        ui.label(f"nominated by {cv.nominator_email}").classes(
                            "text-xs text-gray-400"
                        )
                    if can_manage and nominating:
                        ui.button(
                            "Remove",
                            on_click=notify_errors(
                                lambda _, c=cid: _remove_candidate(proposal_id, c)
                            ),
                        ).props("dense flat color=negative")
                    if can_manage and phase is Phase.concluded:
                        ui.button(
                            "Appoint",
                            icon="verified",
                            on_click=lambda _, c=cid: appoint(c),
                        ).props(
                            "dense"
                            + (
                                ""
                                if view.tally and view.tally.winner_id == cid
                                else " outline"
                            )
                        )
                if cv.candidate.note:
                    ui.label(cv.candidate.note).classes("text-sm text-gray-600")
                with ui.row().classes("items-center gap-1 flex-wrap"):
                    ui.label("Current commitments:").classes("text-xs text-gray-500")
                    if not cv.assignments:
                        ui.label("none").classes("text-xs text-gray-500")
                    for m, t in cv.assignments:
                        ui.badge(f"{t.name} · {ROLE_LABELS[m.role]}").props("outline")

        if nominating and (can_manage or is_voter):
            with ui.row().classes("w-full items-center gap-2"):
                # label must not contain the button text "Nominate": the UI
                # tests match elements by content substring
                who = (
                    ui.select(volunteer_options, label="New candidate", with_input=True)
                    .props("outlined dense")
                    .classes("w-64")
                )
                why = ui.input("Why them?").props("outlined dense").classes("grow")

                @notify_errors
                async def nominate() -> None:
                    if not who.value:
                        ui.notify("Pick a volunteer", color="warning")
                        return
                    async with action_session() as (session, actor):
                        current = await planning_service.get(session, proposal_id)
                        if current is None:
                            raise LookupError("proposal vanished")
                        require(
                            actor.can_manage_team(current.team_id)
                            or proposal_id in actor.voter_proposal_ids,
                            "nominate on this proposal",
                        )
                        await planning_service.add_candidate(
                            session,
                            proposal_id,
                            volunteer_id=who.value,
                            nominated_by=actor.user.id,
                            note=why.value,
                        )
                    ui.navigate.reload()

                ui.button("Nominate", icon="person_add", on_click=nominate).props(
                    "dense"
                )

        ui.label("Voting members").classes("text-lg font-medium mt-2")
        voted = sum(1 for vv in view.voters if vv.has_voted)
        ui.label(f"{voted} of {len(view.voters)} ballots cast").classes(
            "text-sm text-gray-600"
        )
        with ui.column().classes("w-full gap-1"):
            for vv in view.voters:
                with ui.row().classes("w-full items-center gap-2"):
                    ui.label(vv.volunteer.full_name)
                    if vv.has_voted:
                        ui.icon("how_to_vote", color="positive").tooltip("Ballot cast")
                    if not vv.has_account:
                        ui.label("no account — cannot vote").classes(
                            "text-xs text-warning"
                        )
                    ui.space()
                    if can_manage and nominating:
                        ui.button(
                            "Remove",
                            on_click=notify_errors(
                                lambda _, v=vv.voter.id: _remove_voter(proposal_id, v)
                            ),
                        ).props("dense flat")
            if can_manage and nominating:
                with ui.row().classes("w-full items-center gap-2"):
                    extra = (
                        ui.select(
                            volunteer_options, label="Add a voter", with_input=True
                        )
                        .props("outlined dense")
                        .classes("w-64")
                    )

                    @notify_errors
                    async def add_voter() -> None:
                        if not extra.value:
                            ui.notify("Pick a volunteer", color="warning")
                            return
                        async with action_session() as (session, actor):
                            current = await planning_service.get(session, proposal_id)
                            if current is None:
                                raise LookupError("proposal vanished")
                            require(
                                actor.can_manage_team(current.team_id),
                                "manage proposals for this team",
                            )
                            await planning_service.add_voter(
                                session,
                                proposal_id,
                                volunteer_id=extra.value,
                                added_by=actor.user.id,
                            )
                        ui.navigate.reload()

                    ui.button("Add voter", icon="person_add", on_click=add_voter).props(
                        "dense"
                    )

        if voting and is_voter:
            ui.label("Your ballot").classes("text-lg font-medium mt-2")
            ui.label(STAR_NOTE).classes("text-sm text-gray-500")
            toggles: dict[int, ui.toggle] = {}
            with ui.column().classes("w-full gap-1"):
                for cv in view.candidates:
                    with ui.row().classes("items-center gap-3"):
                        ui.label(cv.volunteer.full_name).classes("w-48")
                        toggles[cv.candidate.id] = ui.toggle(
                            {n: str(n) for n in range(6)},
                            value=my.get(cv.candidate.id, 0),
                        ).props("dense")

            @notify_errors
            async def submit_ballot() -> None:
                scores = {c: t.value or 0 for c, t in toggles.items()}
                async with action_session() as (session, actor):
                    require(
                        actor.volunteer_id is not None
                        and proposal_id in actor.voter_proposal_ids,
                        "vote on this proposal",
                    )
                    await planning_service.cast_ballot(
                        session,
                        proposal_id,
                        voter_volunteer_id=actor.volunteer_id,
                        scores=scores,
                    )
                ui.notify(
                    f"Ballot recorded — you may revise it until {p.voting_deadline}",
                    color="positive",
                )
                ui.navigate.reload()

            ui.button("Submit ballot", icon="how_to_vote", on_click=submit_ballot)
        elif voting:
            ui.label(
                "Voting is in progress. The tally appears once voting closes."
            ).classes("text-sm text-gray-500")

        if view.tally:
            _result_section(view.tally, names)
            if can_manage and phase is Phase.concluded:
                ui.button(
                    "Start new round", icon="restart_alt", on_click=new_round_dialog
                ).props("outline")


def _result_section(tally: StarResult, names: dict[int, str]) -> None:
    ui.label("Result").classes("text-lg font-medium mt-2")
    count = tally.ballot_count
    ui.label(f"{count} ballot{'s' if count != 1 else ''} cast").classes(
        "text-sm text-gray-600"
    )
    if tally.winner_id is not None:
        ui.label(f"STAR winner: {names.get(tally.winner_id, '?')}").classes(
            "font-medium text-positive"
        )
    elif tally.tie:
        tied = ", ".join(names.get(c, "?") for c in tally.tied_ids)
        ui.label(f"Tie between {tied}").classes("font-medium text-warning")
    with ui.column().classes("w-full gap-1"):
        for cid, total in sorted(tally.totals.items(), key=lambda kv: -kv[1]):
            with ui.row().classes("w-full items-center gap-2"):
                ui.label(names.get(cid, "?")).classes("w-48")
                ui.label(f"{total} points")
                if tally.finalist_ids and cid in tally.finalist_ids:
                    ui.badge("finalist", color="primary")
                    if tally.runoff is not None:
                        ui.label(f"preferred on {tally.runoff[cid]} ballots").classes(
                            "text-sm text-gray-600"
                        )
    if tally.no_preference:
        ui.label(
            f"{tally.no_preference} ballot(s) had no preference between the finalists"
        ).classes("text-sm text-gray-600")
    ui.label(
        "The tally is advisory: debate together, then appoint — or start a new round."
    ).classes("text-sm text-gray-500 italic")


async def _remove_candidate(proposal_id: int, candidate_id: int) -> None:
    async with action_session() as (session, actor):
        proposal = await planning_service.get(session, proposal_id)
        if proposal is None:
            raise LookupError("proposal vanished")
        require(actor.can_manage_team(proposal.team_id), "manage this proposal")
        await planning_service.remove_candidate(session, proposal_id, candidate_id)
    ui.navigate.reload()


async def _remove_voter(proposal_id: int, voter_id: int) -> None:
    async with action_session() as (session, actor):
        proposal = await planning_service.get(session, proposal_id)
        if proposal is None:
            raise LookupError("proposal vanished")
        require(actor.can_manage_team(proposal.team_id), "manage this proposal")
        await planning_service.remove_voter(session, proposal_id, voter_id)
    ui.navigate.reload()
