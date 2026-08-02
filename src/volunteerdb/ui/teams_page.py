from decimal import Decimal

from nicegui import ui

from ..models import ROLE_LABELS, TeamRole
from ..services import memberships as membership_service
from ..services import reports as report_service
from ..services import teams as team_service
from ..services import volunteers as volunteer_service
from ..sheets import exporter
from .context import (
    action_session,
    asof_banner,
    notify_errors,
    page_session,
    parse_as_of,
)
from .layout import frame
from .volunteer_panel import VolunteerPanel

ROLE_OPTIONS = {role.value: ROLE_LABELS[role] for role in TeamRole}


def _hierarchy_rows(all_teams, coverage, actor) -> list[dict]:
    """One row per team in depth-first order, so every child sits directly under
    its parent and `depth` can indent it into the shape the old tree drew.

    Counts are blanked server-side rather than hidden client-side: a Quasar
    column the browser does not render still receives its row data, so a hidden
    column would ship every team's headcounts to every signed-in member.
    """
    by_parent = team_service.children_map(all_teams)
    paths = team_service.team_paths(all_teams)
    by_team = {r.team.id: r for r in coverage}
    rows: list[dict] = []
    seen: set[int] = set()

    def emit(team, depth: int) -> None:
        # cycles cannot occur live, but as-of snapshots are unvalidated
        if team.id in seen:
            return
        seen.add(team.id)
        row = {
            "id": team.id,
            "order": len(rows),
            "name": team.name,
            "path": paths[team.id],
            "depth": depth,
            "inactive": not team.is_active,
        }
        # coverage() is empty for actors who manage nothing, and skips inactive teams
        r = by_team.get(team.id) if actor.can_manage_team(team.id) else None
        if r is None:
            row |= dict.fromkeys(("leader", "second", "core", "member", "total"), "")
            row |= {"gaps": "", "gap_leader": False, "gap_second": False}
        else:
            row |= {
                "leader": r.counts.get(TeamRole.leader, 0),
                "second": r.counts.get(TeamRole.second, 0),
                "core": r.counts.get(TeamRole.core, 0),
                "member": r.counts.get(TeamRole.member, 0),
                "total": r.total,
                "gaps": int(r.missing_leader) + int(r.missing_second),
                "gap_leader": r.missing_leader,
                "gap_second": r.missing_second,
            }
        rows.append(row)
        for child in by_parent.get(team.id, []):
            emit(child, depth + 1)

    for team in by_parent.get(None, []):
        emit(team, 0)
    for team in all_teams:
        # parent outside this snapshot: list it as a root rather than drop it
        emit(team, 0)
    return rows


@ui.page("/teams")
async def teams_page(as_of: str = ""):
    at = parse_as_of(as_of)
    async with page_session() as (session, actor):
        all_teams = await team_service.list_all(session, at)
        show_coverage = actor.is_admin or bool(actor.managed_team_ids)
        coverage = (
            await report_service.coverage(session, at, teams=all_teams)
            if show_coverage
            else []
        )

    rows = _hierarchy_rows(all_teams, coverage, actor)

    suffix = f"?as_of={as_of}" if as_of else ""
    with frame("Teams", actor):
        asof_banner(at, "/teams")
        if actor.is_admin and at is None:
            with ui.row().classes("gap-2"):
                ui.button(
                    "New team", icon="add", on_click=lambda: _team_dialog(all_teams)
                ).props("dense")
        columns = [
            {
                "name": "team",
                "label": "Team",
                # sorted on the depth-first ordinal, not the name: clicking "Team"
                # restores the hierarchy instead of flattening it into an A-Z list.
                "field": "order",
                "align": "left",
                "sortable": True,
            }
        ]
        if show_coverage:
            columns += [
                {
                    "name": "leader",
                    "label": ROLE_LABELS[TeamRole.leader],
                    "field": "leader",
                },
                {
                    "name": "second",
                    "label": ROLE_LABELS[TeamRole.second],
                    "field": "second",
                },
                {"name": "core", "label": ROLE_LABELS[TeamRole.core], "field": "core"},
                {
                    "name": "member",
                    "label": ROLE_LABELS[TeamRole.member],
                    "field": "member",
                },
                {"name": "total", "label": "Total", "field": "total", "sortable": True},
                # A hierarchy cannot also honour coverage()'s holes-first row
                # order, so the holes become a column here: sort descending to
                # float them up. Chasing them is /planning's job now anyway.
                {"name": "gaps", "label": "Gaps", "field": "gaps", "sortable": True},
            ]
        # no pagination: the tree showed the whole parish at once and this replaces it
        table = ui.table(
            columns=columns, rows=rows, row_key="id", pagination=0
        ).classes("w-full vdb-clickable-rows")
        table.add_slot(
            "body-cell-team",
            """
            <q-td key="team" :props="props"
                  :style="{paddingLeft: (16 + props.row.depth * 22) + 'px'}">
                <span v-if="props.row.depth" class="text-grey-5 q-mr-xs">└</span>
                {{ props.row.name }}
                <q-badge v-if="props.row.inactive" color="grey" class="q-ml-sm">
                    inactive
                </q-badge>
                <q-tooltip v-if="props.row.depth">{{ props.row.path }}</q-tooltip>
            </q-td>
            """,
        )
        if show_coverage:
            table.add_slot(
                "body-cell-gaps",
                """
                <q-td key="gaps" :props="props">
                    <q-badge v-if="props.row.gap_leader" color="warning">
                        no leader
                    </q-badge>
                    <q-badge v-if="props.row.gap_second" color="warning" class="q-ml-xs">
                        no second
                    </q-badge>
                </q-td>
                """,
            )
        table.on(
            "rowClick",
            lambda e: ui.navigate.to(f"/teams/{e.args[1]['id']}{suffix}"),
        )
        if not all_teams:
            ui.label("No teams yet.").classes("text-gray-500")


def _team_dialog(all_teams, team=None) -> None:
    """Create (team=None) or edit a team. Admin only — enforced server-side on save."""
    paths = team_service.team_paths(all_teams)
    parent_options = {0: "— top level —"} | {t.id: paths[t.id] for t in all_teams}
    with ui.dialog() as dialog, ui.card().classes("w-96 gap-3"):
        ui.label("Edit team" if team else "New team").classes("text-lg font-medium")
        name = (
            ui.input("Name", value=team.name if team else "")
            .props("outlined dense")
            .classes("w-full")
        )
        parent = (
            ui.select(
                parent_options,
                label="Parent team",
                value=(team.parent_team_id or 0) if team else 0,
            )
            .props("outlined dense")
            .classes("w-full")
        )
        description = (
            ui.textarea("Description", value=(team.description or "") if team else "")
            .props("outlined dense")
            .classes("w-full")
        )
        weight = (
            ui.number(
                "Workload weight (optional)",
                value=float(team.workload_weight)
                if team is not None and team.workload_weight is not None
                else None,
                min=0,
                step=0.5,
            )
            .props("outlined dense clearable")
            .classes("w-full")
        )
        weight.tooltip(
            "How work-heavy this ministry is; leave empty to exclude it from workload scores"
        )

        @notify_errors
        async def save() -> None:
            async with action_session() as (session, actor):
                from ..permissions import require

                require(actor.is_admin, "only admins manage teams")
                parent_id = parent.value or None
                weight_value = (
                    Decimal(str(weight.value)) if weight.value is not None else None
                )
                if team is None:
                    created = await team_service.create(
                        session,
                        name.value,
                        parent_id,
                        description.value or None,
                        workload_weight=weight_value,
                    )
                    team_id = created.id
                else:
                    await team_service.update(
                        session,
                        team.id,
                        name=name.value,
                        parent_team_id=parent_id,
                        description=description.value or None,
                        workload_weight=weight_value,
                    )
                    team_id = team.id
            dialog.close()
            ui.navigate.to(f"/teams/{team_id}")

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=save)
    dialog.open()


@ui.page("/teams/{team_id}")
async def team_detail(team_id: int, as_of: str = ""):
    at = parse_as_of(as_of)
    async with page_session() as (session, actor):
        team = await team_service.get(session, team_id, at=at)
        if team is None:
            with frame("Team not found", actor):
                ui.label(f"No team with id {team_id} at this time.")
            return
        all_teams = await team_service.list_all(session, at=at)
        paths = team_service.team_paths(all_teams)
        can_names = actor.can_view_roster_names(team_id)
        can_full = actor.can_view_full_roster(team_id)
        can_manage = actor.can_manage_team(team_id) and at is None
        roster = await team_service.roster(session, team_id, at=at) if can_names else []
        children = [t for t in all_teams if t.parent_team_id == team_id]
        volunteer_options = (
            {v.id: v.full_name for v in await volunteer_service.search(session)}
            if can_manage
            else {}
        )

    panel = VolunteerPanel(as_of)
    with frame(paths.get(team_id, team.name), actor):
        asof_banner(at, f"/teams/{team_id}")
        if team.description:
            ui.label(team.description).classes("text-gray-600")
        if not team.is_active:
            ui.badge("inactive", color="grey")

        with ui.row().classes("gap-2"):
            if actor.is_admin and at is None:
                ui.button(
                    "Edit team",
                    icon="edit",
                    on_click=lambda: _team_dialog(
                        [t for t in all_teams if t.id != team_id], team
                    ),
                ).props("dense outline")
                ui.button(
                    "Delete", icon="delete", on_click=lambda: _delete_team(team_id)
                ).props("dense outline color=negative")
            if can_full:
                slug = team.name.lower().replace(" ", "-")

                async def export_xlsx() -> None:
                    async with action_session() as (session, _):
                        content = await exporter.export_workbook(
                            session, team_id=team_id, at=at
                        )
                    ui.download(content, f"{slug}.xlsx")

                async def export_csv(sheet: str) -> None:
                    async with action_session() as (session, _):
                        content = await exporter.export_csv(
                            session, sheet, team_id=team_id, at=at
                        )
                    ui.download(content, f"{slug}-{sheet}.csv")

                with ui.dropdown_button("Export roster", icon="download").props(
                    "dense outline"
                ):
                    ui.item("Excel workbook (.xlsx)", on_click=export_xlsx)
                    ui.item("volunteers.csv", on_click=lambda: export_csv("volunteers"))
                    ui.item(
                        "memberships.csv", on_click=lambda: export_csv("memberships")
                    )

        if children:
            ui.label("Sub-teams").classes("text-lg font-medium")
            with ui.row().classes("gap-2"):
                for child in children:
                    ui.button(
                        child.name,
                        on_click=lambda _, cid=child.id: ui.navigate.to(
                            f"/teams/{cid}"
                        ),
                    ).props("outline dense")

        ui.label("Roster").classes("text-lg font-medium")
        if not can_names:
            ui.label(
                "You are not on this team, so its roster is not visible to you."
            ).classes("text-gray-500")
        elif not roster:
            ui.label("Nobody on this team yet.").classes("text-gray-500")
        else:
            for membership, volunteer in roster:
                with ui.row().classes(
                    "w-full items-center gap-3 p-2 rounded hover:bg-gray-100"
                ):
                    ui.label(volunteer.full_name).classes(
                        "font-medium w-48 cursor-pointer text-primary hover:underline"
                    ).on("click", lambda _, vid=volunteer.id: panel.open(vid))
                    if can_manage:
                        role_select = ui.select(
                            ROLE_OPTIONS, value=membership.role.value
                        ).props("dense outlined")
                        role_select.on_value_change(
                            notify_errors(
                                lambda e, mid=membership.id: _change_role(mid, e.value)
                            )
                        )
                    else:
                        ui.badge(ROLE_LABELS[membership.role])
                    if can_full:
                        ui.label(volunteer.email or "").classes(
                            "text-sm text-gray-600 w-56"
                        )
                        ui.label(volunteer.phone or "").classes("text-sm text-gray-600")
                    ui.space()
                    if membership.joined_on:
                        ui.label(f"since {membership.joined_on.isoformat()}").classes(
                            "text-xs text-gray-400"
                        )
                    if can_manage:
                        ui.button(
                            icon="person_remove",
                            on_click=notify_errors(
                                lambda _, mid=membership.id: _remove_member(mid)
                            ),
                        ).props("dense flat color=negative").tooltip("Remove from team")

        if can_manage:
            ui.label("Add member").classes("text-lg font-medium")
            with ui.row().classes("items-center gap-2"):
                who = (
                    ui.select(volunteer_options, label="Volunteer", with_input=True)
                    .props("outlined dense")
                    .classes("w-64")
                )
                role = (
                    ui.select(ROLE_OPTIONS, label="Role", value=TeamRole.member.value)
                    .props("outlined dense")
                    .classes("w-52")
                )

                @notify_errors
                async def add() -> None:
                    if not who.value:
                        ui.notify("Pick a volunteer", color="warning")
                        return
                    async with action_session() as (session, actor):
                        from ..permissions import require

                        require(
                            actor.can_manage_team(team_id), "manage this team's roster"
                        )
                        await membership_service.assign(
                            session, who.value, team_id, TeamRole(role.value)
                        )
                    ui.navigate.reload()

                ui.button("Add", icon="person_add", on_click=add).props("dense")


async def _change_role(membership_id: int, role_value: str) -> None:
    async with action_session() as (session, actor):
        from ..permissions import require

        membership = await membership_service.get(session, membership_id)
        if membership is None:
            raise LookupError("membership vanished")
        require(actor.can_manage_team(membership.team_id), "manage this team's roster")
        membership.role = TeamRole(role_value)
        await session.flush()
    ui.notify("Role updated", color="positive")


async def _remove_member(membership_id: int) -> None:
    async with action_session() as (session, actor):
        from ..permissions import require

        membership = await membership_service.get(session, membership_id)
        if membership is None:
            raise LookupError("membership vanished")
        require(actor.can_manage_team(membership.team_id), "manage this team's roster")
        await membership_service.remove(session, membership_id)
    ui.navigate.reload()


@notify_errors
async def _delete_team(team_id: int) -> None:
    async with action_session() as (session, actor):
        from ..permissions import require

        require(actor.is_admin, "only admins delete teams")
        await team_service.delete(session, team_id)
    ui.navigate.to("/teams")
