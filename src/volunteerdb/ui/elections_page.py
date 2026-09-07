"""Elections: vacancies + the nomination and STAR-voting pipeline.

/elections lists vacancies (managers can open a proposal from one) and the
proposals the actor may see; /elections/{id} is one proposal's workroom:
candidates with their current commitments (the overwork check), the voting
roll with turnout, the ballot form during the voting phase, and the tally
plus appoint/new-round actions once voting has concluded. Permission gates
mirror the API: managers run the seat, voting members nominate and vote,
and every action handler re-checks inside its own unit of work.

Both pages read as outlines. The workroom loads readmodels.proposal_workroom
-- the proposal, and what this reader may do with it -- and draws one call
per section. Sections, dialogs and handlers are module-level and take ids
and the rows they draw.
"""

from datetime import date, timedelta

from nicegui import ui

from ..env import current as current_env
from ..errors import NotFound
from ..fp import Err
from ..models import ROLE_LABELS, Proposal, ProposalStatus, TeamRole
from ..services import elections as elections_service
from ..services import readmodels
from ..services import volunteers as volunteer_service
from ..services.readmodels import ProposalWorkroom
from ..services.reports import CoverageRow
from ..star import StarResult
from .context import PageCtx, page_ctx, run_command
from .date_input import date_input
from .forms import actions, confirm, dialog_card
from .layout import frame
from .widgets import ROLE_OPTIONS, phase_badge, role_badge, workload_badge

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

Phase = elections_service.ProposalPhase


def _deadline_inputs(d1_default: date, d2_default: date) -> tuple[ui.input, ui.input]:
    d1 = date_input("Nominations close (YYYY-MM-DD)", value=str(d1_default)).classes(
        "w-full"
    )
    d2 = date_input("Voting closes (YYYY-MM-DD)", value=str(d2_default)).classes(
        "w-full"
    )
    return d1, d2


def _parse_deadlines(d1: ui.input, d2: ui.input) -> tuple[date, date] | None:
    try:
        return date.fromisoformat(d1.value or ""), date.fromisoformat(d2.value or "")
    except ValueError:
        ui.notify("Deadlines must be YYYY-MM-DD dates", color="warning")
        return None


# --- the listing -----------------------------------------------------------------


def _summary_row(s: elections_service.ProposalSummary) -> None:
    p = s.proposal
    with ui.row().classes("w-full items-center gap-2 p-2 rounded bg-gray-50"):
        ui.link(
            f"{s.path}: {ROLE_LABELS[TeamRole(p.role)]}", f"/elections/{p.id}"
        ).classes("font-medium")
        phase_badge(p, s.phase)
        ui.space()
        ui.label(
            f"{s.candidate_count} candidate{'s' if s.candidate_count != 1 else ''}"
            f" · {s.voted_count}/{s.voter_count} ballots"
        ).classes("text-sm text-gray-600")


def _create_proposal_dialog(
    team_id: int, path: str, default_role: TeamRole, volunteer_options: dict[int, str]
) -> None:
    with dialog_card(f"Propose for {path}", width="w-[28rem]") as dialog:
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
        today = current_env().today()
        d1, d2 = _deadline_inputs(
            today + timedelta(days=14), today + timedelta(days=28)
        )
        # The API has always accepted notes on create and on patch; the GUI
        # displayed them and could never write one, so the proposer's own
        # framing of the seat could only be set over JSON.
        notes = (
            ui.textarea("Notes (what is this seat, and why now?)")
            .props("outlined dense autogrow")
            .classes("w-full")
        )
        notes.tooltip(
            "Shown to the voting roll on the proposal page — the case for "
            "the seat, not for a candidate"
        )
        ui.label(
            "The voting roll is prefilled: this team's leader, second and "
            "core members, plus the clergy team. Voting members may add "
            "candidates until nominations close."
        ).classes("text-xs text-gray-500")

        async def save() -> None:
            if not who.value:
                ui.notify("Pick the first candidate", color="warning")
                return
            if (deadlines := _parse_deadlines(d1, d2)) is None:
                return

            async def command(ctx: PageCtx):
                return await elections_service.create_proposal(
                    ctx.session,
                    ctx.actor,
                    team_id=team_id,
                    role=TeamRole(role.value),
                    nomination_deadline=deadlines[0],
                    voting_deadline=deadlines[1],
                    created_by=ctx.actor.account.id,
                    candidates=[elections_service.CandidateInput(who.value, why.value)],
                    notes=notes.value or None,
                    today=ctx.env.today(),
                )

            def done(proposal, _effects, _report) -> None:
                dialog.close()
                ui.navigate.to(f"/elections/{proposal.id}")

            await run_command(command, on_ok=done, reload=False)

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            # not "Open proposal": that is a substring of the section
            # header "Open proposals" and would confuse content matching
            ui.button("Create proposal", icon="how_to_vote", on_click=save)
    dialog.open()


def _vacancies_section(
    vacancies: list[CoverageRow],
    proposal_team_ids: set[int],
    volunteer_options: dict[int, str],
) -> None:
    """Teams missing a leader or a second, each with the button that opens a
    proposal for the missing seat."""
    ui.label("Vacancies").classes("text-lg font-medium mt-4")
    if not vacancies:
        ui.label("Every team has a leader and a second-in-command. 🎉").classes(
            "text-positive"
        )
    for r in vacancies:
        seat = TeamRole.leader if r.missing_leader else TeamRole.second
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
                ui.label(f"{r.total} member{'s' if r.total != 1 else ''}").classes(
                    "text-sm text-gray-600"
                )
                ui.button(
                    "Start proposal",
                    icon="how_to_vote",
                    on_click=lambda _, tid=r.team.id, path=r.path, role=seat: (
                        _create_proposal_dialog(tid, path, role, volunteer_options)
                    ),
                ).props("dense outline")


@ui.page("/elections")
async def elections_page():
    async with page_ctx() as ctx:
        session, actor = ctx.session, ctx.actor
        allowed = actor.can_access_elections
        can_create = allowed and (actor.is_admin or bool(actor.managed_team_ids))
        vacancy_rows = (
            await elections_service.vacancies(session, actor) if can_create else []
        )
        summaries = (
            await elections_service.list_proposals(
                session, actor, today=ctx.env.today()
            )
            if allowed
            else []
        )
        volunteer_options = (
            await volunteer_service.name_map(session) if can_create else {}
        )
    if not allowed:
        with frame("Elections", actor):
            ui.label(
                "Elections are available to admins, team leaders/seconds, "
                "and the voting members of a proposal."
            ).classes("text-gray-500")
        return

    open_rows = [s for s in summaries if s.proposal.status == ProposalStatus.open.value]
    decided_rows = [
        s for s in summaries if s.proposal.status != ProposalStatus.open.value
    ]
    proposal_team_ids = {s.proposal.team_id for s in open_rows}

    with frame("Elections", actor):
        if open_rows:
            ui.label("Open proposals").classes("text-lg font-medium")
            with ui.column().classes("w-full gap-1"):
                for s in open_rows:
                    _summary_row(s)
        elif not can_create:
            ui.label("No open proposals need you right now.").classes("text-gray-500")

        if can_create:
            _vacancies_section(vacancy_rows, proposal_team_ids, volunteer_options)

        if decided_rows:
            ui.label("Recently decided").classes("text-lg font-medium mt-4")
            with ui.column().classes("w-full gap-1"):
                for s in decided_rows[:20]:
                    _summary_row(s)


# --- the workroom's dialogs and actions ----------------------------------------
#
# Each takes ids, not page state; the service it calls decides whether the
# actor may do it, and run_command toasts the refusal.


def _edit_proposal_dialog(proposal: Proposal) -> None:
    with dialog_card("Edit proposal", width="w-[30rem]") as dialog:
        d1, d2 = _deadline_inputs(
            proposal.nomination_deadline, proposal.voting_deadline
        )
        notes = (
            ui.textarea("Notes", value=proposal.notes or "")
            .props("outlined dense autogrow")
            .classes("w-full")
        )

        async def save() -> None:
            if (deadlines := _parse_deadlines(d1, d2)) is None:
                return
            dialog.close()

            async def command(ctx: PageCtx):
                return await elections_service.update_proposal(
                    ctx.session,
                    ctx.actor,
                    proposal.id,
                    nomination_deadline=deadlines[0],
                    voting_deadline=deadlines[1],
                    notes=notes.value or None,
                    today=ctx.env.today(),
                )

            await run_command(command)

        actions(dialog, "Save", save)
    dialog.open()


async def _cancel_proposal(proposal_id: int) -> None:
    if not await confirm(
        "Cancel this proposal? Ballots are discarded with it.",
        yes="Yes, cancel it",
        no="Keep it",
        danger=True,
    ):
        return

    async def command(ctx: PageCtx):
        return await elections_service.cancel(
            ctx.session,
            ctx.actor,
            proposal_id,
            decided_by=ctx.actor.account.id,
            now=ctx.now,
        )

    await run_command(command)


async def _appoint(
    proposal_id: int, candidate_id: int, *, name: str, role_label: str
) -> None:
    if not await confirm(
        f"Appoint {name} as {role_label}? This assigns the role immediately.",
        yes="Yes, appoint",
        no="Back",
    ):
        return

    async def command(ctx: PageCtx):
        return await elections_service.appoint(
            ctx.session,
            ctx.actor,
            proposal_id,
            candidate_id,
            decided_by=ctx.actor.account.id,
            today=ctx.env.today(),
            now=ctx.now,
        )

    await run_command(command)


def _new_round_dialog(proposal_id: int) -> None:
    with dialog_card("Start a new round") as dialog:
        ui.label("Candidates and the voting roll carry over; ballots do not.").classes(
            "text-sm text-gray-500"
        )
        today = current_env().today()
        d1, d2 = _deadline_inputs(
            today + timedelta(days=14), today + timedelta(days=28)
        )

        async def save() -> None:
            if (deadlines := _parse_deadlines(d1, d2)) is None:
                return

            async def command(ctx: PageCtx):
                return await elections_service.new_round(
                    ctx.session,
                    ctx.actor,
                    proposal_id,
                    created_by=ctx.actor.account.id,
                    nomination_deadline=deadlines[0],
                    voting_deadline=deadlines[1],
                    today=ctx.env.today(),
                    now=ctx.now,
                )

            def done(fresh, _effects, _report) -> None:
                dialog.close()
                ui.navigate.to(f"/elections/{fresh.id}")

            await run_command(command, on_ok=done, reload=False)

        actions(dialog, "Start round", save, icon="restart_alt")
    dialog.open()


async def _nominate(proposal_id: int, volunteer_id: int | None, note: str) -> None:
    if not volunteer_id:
        ui.notify("Pick a volunteer", color="warning")
        return

    async def command(ctx: PageCtx):
        return await elections_service.add_candidate(
            ctx.session,
            ctx.actor,
            proposal_id,
            volunteer_id=volunteer_id,
            nominated_by=ctx.actor.account.id,
            note=note,
            today=ctx.env.today(),
        )

    await run_command(command, reload=True)


async def _remove_candidate(proposal_id: int, candidate_id: int) -> None:
    await run_command(
        lambda ctx: elections_service.remove_candidate(
            ctx.session, ctx.actor, proposal_id, candidate_id, today=ctx.env.today()
        )
    )


async def _add_voter(proposal_id: int, volunteer_id: int | None) -> None:
    if not volunteer_id:
        ui.notify("Pick a volunteer", color="warning")
        return

    async def command(ctx: PageCtx):
        return await elections_service.add_voter(
            ctx.session,
            ctx.actor,
            proposal_id,
            volunteer_id=volunteer_id,
            added_by=ctx.actor.account.id,
            today=ctx.env.today(),
        )

    await run_command(command, reload=True)


async def _remove_voter(proposal_id: int, voter_id: int) -> None:
    await run_command(
        lambda ctx: elections_service.remove_voter(
            ctx.session, ctx.actor, proposal_id, voter_id, today=ctx.env.today()
        )
    )


async def _cast_ballot(
    proposal_id: int, scores: dict[int, int], *, voting_deadline: date
) -> None:
    async def command(ctx: PageCtx):
        return await elections_service.cast_ballot(
            ctx.session,
            ctx.actor,
            proposal_id,
            scores=scores,
            today=ctx.env.today(),
            now=ctx.now,
        )

    def done(_value, _effects, _report) -> None:
        ui.notify(
            f"Ballot recorded — you may revise it until {voting_deadline}",
            color="positive",
        )

    await run_command(command, on_ok=done, reload=True)


# --- the workroom's sections ---------------------------------------------------
#
# One function per block on /elections/{id}, in the order the page draws
# them; each takes the room (readmodels.proposal_workroom) or the rows it
# draws.


def _proposal_header(room: ProposalWorkroom) -> None:
    """Team, role and phase, the manager's Edit and Cancel while the proposal
    is open, the two deadlines, who opened and decided it, the notes, and
    the note on how an Ignatian election is run."""
    p = room.proposal
    with ui.row().classes("w-full items-center gap-2"):
        ui.link(room.view.path, f"/teams/{p.team_id}").classes("font-medium")
        role_badge(TeamRole(p.role))
        phase_badge(p, room.phase)
        ui.space()
        if room.can_manage and p.status == ProposalStatus.open.value:
            ui.button(
                "Edit deadlines & notes",
                icon="edit_calendar",
                on_click=lambda: _edit_proposal_dialog(p),
            ).props("dense outline")
            ui.button("Cancel proposal", on_click=lambda: _cancel_proposal(p.id)).props(
                "dense outline color=negative"
            )
    with ui.row().classes("w-full gap-4 text-sm text-gray-600"):
        ui.label(f"Nominations close {p.nomination_deadline}")
        ui.label(f"Voting closes {p.voting_deadline}")
        if room.view.creator_email:
            ui.label(f"opened by {room.view.creator_email}")
        if p.decided_at is not None and room.view.decider_email:
            ui.label(f"decided by {room.view.decider_email}")
    if p.notes:
        ui.label(p.notes).classes("text-gray-600")

    with ui.card().classes("w-full p-3"):
        with ui.row().classes("items-center gap-2 no-wrap"):
            ui.icon("campaign", size="sm").classes("text-primary")
            ui.label(IGNATIAN_NOTE).classes("text-sm italic")


def _candidate_card(
    room: ProposalWorkroom, cv: elections_service.CandidateView
) -> None:
    """One candidate: their badges (appointed, STAR winner, workload), who
    nominated them, the manager's Remove or Appoint, the nomination note and
    their current commitments -- the overwork check."""
    p = room.proposal
    cid = cv.candidate.id
    winner = room.view.tally is not None and room.view.tally.winner_id == cid
    with ui.card().classes("w-full gap-2 p-3"):
        with ui.row().classes("w-full items-center gap-2"):
            ui.link(cv.volunteer.full_name, f"/volunteers/{cv.volunteer.id}").classes(
                "font-medium"
            )
            if p.appointed_candidate_id == cid:
                ui.badge("Appointed", color="positive")
            if winner:
                ui.badge("STAR winner", color="primary")
            if cv.volunteer.id in room.workload:
                workload_badge(
                    *room.workload[cv.volunteer.id], tooltip="Current workload"
                )
            ui.space()
            if cv.nominator_email:
                ui.label(f"nominated by {cv.nominator_email}").classes(
                    "text-xs text-gray-400"
                )
            if room.can_manage and room.phase is Phase.nominating:
                ui.button(
                    "Remove",
                    on_click=lambda _, c=cid: _remove_candidate(p.id, c),
                ).props("dense flat color=negative")
            if room.can_manage and room.phase is Phase.concluded:
                ui.button(
                    "Appoint",
                    icon="verified",
                    on_click=lambda _, c=cid: _appoint(
                        p.id,
                        c,
                        name=room.names[c],
                        role_label=ROLE_LABELS[TeamRole(p.role)],
                    ),
                ).props("dense" + ("" if winner else " outline"))
        if cv.candidate.note:
            ui.label(cv.candidate.note).classes("text-sm text-gray-600")
        with ui.row().classes("items-center gap-1 flex-wrap"):
            ui.label("Current commitments:").classes("text-xs text-gray-500")
            if not cv.assignments:
                ui.label("none").classes("text-xs text-gray-500")
            for m, t in cv.assignments:
                ui.badge(f"{t.name} · {ROLE_LABELS[m.role]}").props("outline")


def _candidates_section(room: ProposalWorkroom) -> None:
    ui.label("Candidates").classes("text-lg font-medium mt-2")
    for cv in room.view.candidates:
        _candidate_card(room, cv)


def _nominate_row(proposal_id: int, volunteer_options: dict[int, str]) -> None:
    """Put a name forward. Open to voters as well as managers: the roll is who
    the parish trusts to weigh this seat, so it is also who may suggest for it."""
    with ui.row().classes("w-full items-center gap-2"):
        # label must not contain the button text "Nominate": the UI
        # tests match elements by content substring
        who = (
            ui.select(volunteer_options, label="New candidate", with_input=True)
            .props("outlined dense")
            .classes("w-64")
        )
        why = ui.input("Why them?").props("outlined dense").classes("grow")
        ui.button(
            "Nominate",
            icon="person_add",
            on_click=lambda: _nominate(proposal_id, who.value, why.value),
        ).props("dense")


def _voters_section(
    proposal_id: int,
    voters: list[elections_service.VoterView],
    volunteer_options: dict[int, str],
    *,
    can_manage: bool,
    nominating: bool,
) -> None:
    """The roll, with turnout. That somebody has voted is shown; what they voted
    is not — ballots are secret, so only the flag and the count appear here."""
    ui.label("Voting members").classes("text-lg font-medium mt-2")
    voted = sum(1 for vv in voters if vv.has_voted)
    ui.label(f"{voted} of {len(voters)} ballots cast").classes("text-sm text-gray-600")
    with ui.column().classes("w-full gap-1"):
        for vv in voters:
            with ui.row().classes("w-full items-center gap-2"):
                ui.label(vv.volunteer.full_name)
                if vv.has_voted:
                    ui.icon("how_to_vote", color="positive").tooltip("Ballot cast")
                if not vv.has_account:
                    ui.label("no account — cannot vote").classes("text-xs text-warning")
                ui.space()
                if can_manage and nominating:
                    ui.button(
                        "Remove",
                        on_click=lambda _, v=vv.voter.id: _remove_voter(proposal_id, v),
                    ).props("dense flat")
        if can_manage and nominating:
            with ui.row().classes("w-full items-center gap-2"):
                extra = (
                    ui.select(volunteer_options, label="Add a voter", with_input=True)
                    .props("outlined dense")
                    .classes("w-64")
                )
                ui.button(
                    "Add voter",
                    icon="person_add",
                    on_click=lambda: _add_voter(proposal_id, extra.value),
                ).props("dense")


def _ballot_section(
    proposal_id: int,
    candidates: list[elections_service.CandidateView],
    mine: dict[int, int],
    voting_deadline: date,
) -> None:
    """One 0-5 toggle per candidate, revisable until the deadline. A candidate
    left alone is submitted as an explicit 0: STAR has no abstention."""
    ui.label("Your ballot").classes("text-lg font-medium mt-2")
    ui.label(STAR_NOTE).classes("text-sm text-gray-500")
    toggles: dict[int, ui.toggle] = {}
    with ui.column().classes("w-full gap-1"):
        for cv in candidates:
            with ui.row().classes("items-center gap-3"):
                ui.label(cv.volunteer.full_name).classes("w-48")
                toggles[cv.candidate.id] = ui.toggle(
                    {n: str(n) for n in range(6)},
                    value=mine.get(cv.candidate.id, 0),
                ).props("dense")
    ui.button(
        "Submit ballot",
        icon="how_to_vote",
        on_click=lambda: _cast_ballot(
            proposal_id,
            {c: t.value or 0 for c, t in toggles.items()},
            voting_deadline=voting_deadline,
        ),
    )


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


@ui.page("/elections/{proposal_id}")
async def proposal_detail(proposal_id: int):
    async with page_ctx() as ctx:
        actor = ctx.actor
        shown = await readmodels.proposal_workroom(
            ctx.session, actor, proposal_id, now=ctx.now, tz=ctx.env.tz
        )
    match shown:
        case Err(NotFound()):
            with frame("Proposal not found", actor):
                ui.label(f"No proposal with id {proposal_id}.")
            return
        case Err():
            # the service decides; the page only chooses how to say it, and
            # a whole page reads better than a toast on an empty frame
            with frame("Elections", actor):
                ui.label(
                    "This proposal is visible to its voting members and to "
                    "the team's managers."
                ).classes("text-gray-500")
            return
    room = shown.value
    p = room.proposal
    nominating = room.phase is Phase.nominating
    voting = room.phase is Phase.voting

    with frame(f"{room.view.path}: {ROLE_LABELS[TeamRole(p.role)]}", actor):
        _proposal_header(room)
        _candidates_section(room)
        if nominating and (room.can_manage or room.is_voter):
            _nominate_row(proposal_id, room.volunteer_options)
        _voters_section(
            proposal_id,
            room.view.voters,
            room.volunteer_options,
            can_manage=room.can_manage,
            nominating=nominating,
        )
        if voting and room.is_voter:
            _ballot_section(
                proposal_id, room.view.candidates, room.my_scores, p.voting_deadline
            )
        elif voting:
            ui.label(
                "Voting is in progress. The tally appears once voting closes."
            ).classes("text-sm text-gray-500")
        if room.view.tally:
            _result_section(room.view.tally, room.names)
            if room.can_manage and room.phase is Phase.concluded:
                ui.button(
                    "Start new round",
                    icon="restart_alt",
                    on_click=lambda: _new_round_dialog(proposal_id),
                ).props("outline")
