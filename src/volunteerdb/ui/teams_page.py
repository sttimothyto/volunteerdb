from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from urllib.parse import quote

import httpx
from fastapi import Request
from nicegui import events, ui

from .. import query_lang
from ..config import settings
from ..models import ROLE_LABELS, TeamPage, TeamRole, TeamSheet
from ..services import elections as elections_service
from ..services import events as event_service
from ..services import mail, roster_sheets
from ..services import memberships as membership_service
from ..services import pages as page_service
from ..services import reports as report_service
from ..services import teams as team_service
from ..services import users as user_service
from ..services import volunteers as volunteer_service
from ..sheets import importer
from ..sheets.common import sheet_url
from . import column_order, invites
from .account_status import roster_account
from .context import (
    action_session,
    notify_errors,
    page_session,
    parse_as_of,
)
from .layout import frame
from .volunteer_panel import VolunteerPanel, volunteer_link

ROLE_OPTIONS = {role.value: ROLE_LABELS[role] for role in TeamRole}


def _hierarchy_rows(tree, coverage, actor) -> list[dict]:
    """One row per team in depth-first order, so every child sits directly under
    its parent and `depth` can indent it into the shape the old tree drew.

    Counts are blanked server-side rather than hidden client-side: a Quasar
    column the browser does not render still receives its row data, so a hidden
    column would ship every team's headcounts to every signed-in member.
    """
    by_parent, paths = tree.by_parent, tree.paths
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
            # not a table column; carried for the search box's query filters
            "description": team.description or "",
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
    for team in tree.teams:
        # parent outside this snapshot: list it as a root rather than drop it
        emit(team, 0)
    return rows


def _with_ancestors(rows: list[dict], keep: set[int]) -> list[dict]:
    """The kept rows plus their ancestors, keeping the tree indent honest: the
    rows that the `depth` indent and the └ prefix hang off must stay, so a hit
    on a child never renders as an orphan sitting at an indent under nothing.
    """
    kept = set(keep)
    for i in keep:
        # rows are depth-first, so a row's ancestors are the nearest preceding
        # rows whose depth keeps stepping down
        depth = rows[i]["depth"]
        for j in range(i - 1, -1, -1):
            if depth == 0:
                break
            if rows[j]["depth"] < depth:
                kept.add(j)
                depth = rows[j]["depth"]
    return [row for i, row in enumerate(rows) if i in kept]


def _matching_rows(rows: list[dict], text: str) -> list[dict]:
    """The rows whose display path contains `text`, plus each match's ancestors.

    Matching the path rather than the bare name lets a parent's name pull in
    its whole subtree.
    """
    keep = {i for i, row in enumerate(rows) if text in row["path"].lower()}
    return _with_ancestors(rows, keep)


def _team_count(shown: int, total: int | None = None) -> str:
    if total is None or shown == total:
        return f"{shown} team{'s' if shown != 1 else ''}"
    return f"{shown} of {total} teams"


def _wire_search(
    search: ui.input, count: ui.label, table: ui.table, rows: list[dict]
) -> None:
    """Narrow the table as you type. The listing has no pagination, so the whole
    parish is already in `rows` and the filter costs neither a query nor a
    reload — it only swaps what the table is showing."""

    def apply() -> None:
        text = (search.value or "").strip()
        ast = query_lang.parse(text) if text else None
        if ast is None:
            shown = rows if not text else _matching_rows(rows, text.lower())
        else:
            try:
                pred = query_lang.compile_teams(ast)
            except query_lang.QueryError as exc:
                # inline, not a toast: this filter runs on every keystroke
                count.set_text(f"query error: {exc}")
                return
            shown = _with_ancestors(rows, {i for i, r in enumerate(rows) if pred(r)})
        table.rows = shown
        table.update()
        count.set_text(_team_count(len(shown), len(rows)))

    search.on_value_change(apply)


@ui.page("/teams")
async def teams_page(as_of: str = ""):
    at = parse_as_of(as_of)
    async with page_session() as (session, actor):
        tree = await team_service.tree(session, at)
        show_coverage = actor.is_admin or bool(actor.managed_team_ids)
        coverage = await report_service.coverage(session, at) if show_coverage else []

    rows = _hierarchy_rows(tree, coverage, actor)

    suffix = f"?as_of={as_of}" if as_of else ""
    for row in rows:
        # bound as row data, not interpolated into the slot template: as_of is
        # a raw query param and must never reach Vue's template compiler
        row["href"] = f"/teams/{row['id']}{suffix}"
    with frame("Teams", actor, as_of=at, asof_path="/teams"):
        with ui.row().classes("items-center gap-2 w-full"):
            search = (
                ui.input("Search teams…")
                .props("outlined dense clearable debounce=200")
                .classes("grow")
                if rows
                else None
            )
            # the search box grows into the free space and holds the buttons
            # against the right edge; with no teams to search there is nothing
            # growing, so the spacer takes over that job
            if search is None:
                ui.space()
            # no permission gate: /ministries/ is the world-readable index the
            # QR codes point at, and this is the only door to it from inside
            ui.button("View Team Homepages", icon="public").props(
                'dense outline href="/ministries/"'
            )
            if actor.is_admin and at is None:
                options = _parent_options(tree)
                ui.button(
                    "New team", icon="add", on_click=lambda: _team_dialog(options)
                ).props("dense")
            # Context-sensitive, and hidden entirely below core: the
            # exporter authorizes every id in the scope anyway, so this only
            # decides whether an unusable button is on screen.
            if actor.is_admin or actor.full_view_team_ids:
                # a link to a route (ui/team_files_route.py), not a handler:
                # the route re-derives the scope inside its own session, as
                # the handler did, and the file is right-click-saveable
                ui.button("Export team(s)", icon="download").props(
                    'dense outline href="/export/teams.csv"'
                )
        columns = [
            {
                "name": "team",
                "label": "Team",
                # sorted on the depth-first ordinal, not the name: clicking "Team"
                # restores the hierarchy instead of flattening it into an A-Z list.
                "field": "order",
                "align": "left",
                "sortable": True,
                # the hierarchy, not a column of data: the body-cell-team slot
                # indents by row depth and hangs a └ off it, which only reads as
                # a tree while it is the leftmost thing on the row
                column_order.FIXED: True,
            }
        ]
        if show_coverage:
            # every count sorts; a blanked cell is "", which Quasar string-compares
            # and so files ahead of every number, keeping the order well-defined
            columns += [
                {
                    "name": "leader",
                    "label": ROLE_LABELS[TeamRole.leader],
                    "field": "leader",
                    "sortable": True,
                },
                {
                    "name": "second",
                    "label": ROLE_LABELS[TeamRole.second],
                    "field": "second",
                    "sortable": True,
                },
                {
                    "name": "core",
                    "label": ROLE_LABELS[TeamRole.core],
                    "field": "core",
                    "sortable": True,
                },
                {
                    "name": "member",
                    "label": ROLE_LABELS[TeamRole.member],
                    "field": "member",
                    "sortable": True,
                },
                {"name": "total", "label": "Total", "field": "total", "sortable": True},
                # A hierarchy cannot also honour coverage()'s holes-first row
                # order, so the holes become a column here: sort descending to
                # float them up. Chasing them is /elections's job now anyway.
                {"name": "gaps", "label": "Gaps", "field": "gaps", "sortable": True},
            ]
        # no pagination: the tree showed the whole parish at once and this replaces it
        columns = column_order.apply_saved_order("teams", columns)
        table = ui.table(
            columns=columns, rows=rows, row_key="id", pagination=0
        ).classes("w-full vdb-clickable-rows")
        column_order.make_draggable(table, "teams")
        table.add_slot(
            "body-cell-team",
            """
            <q-td key="team" :props="props"
                  :style="{paddingLeft: (16 + props.row.depth * 22) + 'px'}">
                <span v-if="props.row.depth" class="text-gray-500 q-mr-xs">└</span>
                <a :href="props.row.href" class="vdb-quiet" @click.stop>
                    {{ props.row.name }}
                </a>
                <q-badge v-if="props.row.inactive" color="muted" class="q-ml-sm">
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
        if not tree.teams:
            ui.label("No teams yet.").classes("text-gray-500")
        count = ui.label(_team_count(len(rows))).classes("text-sm text-gray-500")
        if search is not None:
            _wire_search(search, count, table, rows)


def _parent_options(tree, exclude_id: int | None = None) -> dict[int, str]:
    """Parent choices for the team dialog, built before the button that opens it.

    The dialog is opened from a click callback that outlives `page_session()`, so
    it captures these plain ids and paths rather than the session's TeamTree —
    which would pin every detached Team instance for the life of the browser tab,
    and rebuild the same options on every open. `exclude_id` drops one team:
    editing a team, it is the team itself, which cannot be its own parent.
    """
    return {0: "— top level —"} | {
        t.id: tree.paths[t.id] for t in tree.teams if t.id != exclude_id
    }


def _team_dialog(parent_options: dict[int, str], team=None) -> None:
    """Create (team=None) or edit a team. Admin only — enforced server-side on save."""
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
                "Workload weight",
                # a new ministry is ordinary work, not zero work, so the box
                # starts at 1 rather than at the 0 that means "excluded". It
                # stays clearable, so excluding one is still a single click.
                value=float(team.workload_weight)
                if team is not None and team.workload_weight is not None
                else 1.0,
                min=0,
                step=0.5,
            )
            .props("outlined dense clearable")
            .classes("w-full")
        )
        weight.tooltip(
            "How work-heavy this ministry is, against a weight of 1. New "
            "ministries start at 1; clear the box to exclude this one from "
            "workload scores"
        )
        # Archiving was reachable over the API (PATCH /teams/{id} is_active) and
        # nowhere in the GUI, which rendered the "inactive" badge it could not
        # produce. A ministry that has wound down is archived rather than
        # deleted: deleting takes its memberships with it, while archiving keeps
        # the history and stops the team appearing where teams are chosen.
        active = None
        if team is not None:
            active = ui.switch("Active", value=team.is_active)
            active.tooltip(
                "Archive a ministry that has wound down: it keeps its history "
                "and stops appearing in pickers. Deleting removes it entirely."
            )

        @notify_errors
        async def save() -> None:
            async with action_session() as (session, actor):
                parent_id = parent.value or None
                weight_value = (
                    Decimal(str(weight.value)) if weight.value is not None else None
                )
                if team is None:
                    created = (
                        await team_service.create(
                            session,
                            actor,
                            name.value,
                            parent_id,
                            description.value or None,
                            workload_weight=weight_value,
                        )
                    ).unwrap()
                    team_id = created.id
                else:
                    (
                        await team_service.update(
                            session,
                            actor,
                            team.id,
                            name=name.value,
                            parent_team_id=parent_id,
                            description=description.value or None,
                            workload_weight=weight_value,
                            is_active=active.value if active is not None else None,
                        )
                    ).unwrap()
                    team_id = team.id
            dialog.close()
            ui.navigate.to(f"/teams/{team_id}")

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=save)
    dialog.open()


def _home_page_section(
    team, team_page, team_id: int, slug: str | None, base_url: str
) -> None:
    """Home-page controls for leaders/seconds/core members (and admins): link a
    public Google Doc, preview-fetch it, and reach the published page."""
    ui.label("Volunteer home page").classes("text-lg font-medium")
    if not team.home_doc_url:
        with ui.row().classes("items-center gap-2"):
            ui.button(
                "Set home page doc",
                icon="add_link",
                on_click=lambda: _home_doc_dialog(team_id, None),
            ).props("dense outline")
            ui.label(
                "Link a public Google Doc to publish this team's page under "
                "/ministries/ — no sign-in needed to read it."
            ).classes("text-sm text-gray-500 vdb-prose")
        return

    published = team_page is not None and team_page.html
    with ui.row().classes("items-center gap-2"):
        ui.link("Google Doc", team.home_doc_url, new_tab=True)
        if published and slug:
            ui.link("Public page", f"/ministries/{slug}.html", new_tab=True)
            ui.button("Download QR Code to Public page", icon="qr_code_2").props(
                f'dense outline href="/teams/{team_id}/qr.png"'
            )
        ui.button(
            "Fetch now",
            icon="refresh",
            on_click=lambda: _fetch_home_page(team_id),
        ).props("dense outline")
        ui.button(
            "Change",
            icon="edit",
            on_click=lambda: _home_doc_dialog(team_id, team.home_doc_url),
        ).props("dense flat")
    if team_page is not None and team_page.status == "error":
        ui.label(f"Last fetch failed: {team_page.error}").classes(
            "text-negative text-sm"
        )
    elif not published:
        ui.label(
            "Not published yet — Fetch now downloads the doc, or wait for the "
            "nightly refresh (3:00)."
        ).classes("text-sm text-gray-500")
    elif team_page.fetched_at is not None:
        ui.label(
            f"Refreshed nightly · last fetched {team_page.fetched_at:%Y-%m-%d %H:%M}"
        ).classes("text-sm text-gray-500")


def _sheet_section(team_sheet: TeamSheet | None, team_id: int, is_admin: bool) -> None:
    """The team's roster spreadsheet, for leaders/seconds (and admins).

    Everything to do with getting rosters in and out of a spreadsheet lives
    here now: the sheet's link, the template to copy, an on-demand sync, and
    the .csv import that used to have a page of its own. Gated on can_manage
    by the caller — the link IS the access to the sheet, so who may see it is
    who may manage the roster.
    """
    ui.label("Roster spreadsheet").classes("text-lg font-medium")
    linked = team_sheet is not None and bool(team_sheet.file_id)
    with ui.row().classes("items-center gap-2"):
        if linked:
            ui.link(
                team_sheet.file_name or "Google Sheet",
                sheet_url(team_sheet.file_id),
                new_tab=True,
            )
            ui.button(
                # not plain "Change": the home-page section above carries one
                # of those, and the two sit a few lines apart
                "Change spreadsheet",
                icon="edit",
                on_click=lambda: _roster_sheet_dialog(team_id, linked=True),
            ).props("dense flat")
            ui.button(
                "Sync now",
                icon="sync",
                on_click=lambda: _sync_sheet(team_id, roster_sheets.IMPORT),
            ).props("dense outline")
            ui.button(
                "Overwrite sheet",
                icon="upload",
                on_click=lambda: _sync_sheet(team_id, roster_sheets.EXPORT),
            ).props("dense flat").tooltip(
                "Rewrites the spreadsheet from the database, discarding "
                "whatever is in it — the way out of a mangled sheet."
            )
        else:
            ui.button(
                "Link a spreadsheet",
                icon="add_link",
                on_click=lambda: _roster_sheet_dialog(team_id, linked=False),
            ).props("dense outline")
        if settings().template_sheet_url:
            # The decorated Google Sheet (role dropdown, hidden ID column,
            # structure warning) replaces the bare CSV: copy it, share the
            # copy, link it here — the decoration comes along with the copy.
            ui.button("Roster template (Google Sheets)", icon="open_in_new").props(
                f'outline dense href="{settings().template_sheet_url}" target="_blank"'
            )
        else:  # dev fallback: no Drive template configured
            ui.button("Empty template", icon="description").props(
                'outline dense href="/export/roster-template.csv"'
            )
    if linked:
        ui.label(
            "Edits sync into the database nightly (2:30), and the sheet is "
            "rewritten to match. Nobody is ever removed by a sync — take a "
            "member off the roster above instead. Anyone holding this link "
            "can edit the sheet, so keep it among the people who help run "
            "this team."
        ).classes("text-sm text-gray-500 vdb-prose")
    else:
        ui.label(
            "The nightly sync (2:30) creates a Google Sheet for this team's "
            "roster; the link will appear here. Or copy the template, share "
            "it as “anyone with the link can edit”, and link it yourself."
        ).classes("text-sm text-gray-500 vdb-prose")
    if team_sheet is not None:
        if team_sheet.last_status == "error":
            ui.label(f"Last sync failed: {team_sheet.last_error}").classes(
                "text-negative text-sm"
            )
        elif team_sheet.last_synced_at is not None:
            ui.label(f"Last synced {team_sheet.last_synced_at:%Y-%m-%d %H:%M}").classes(
                "text-sm text-gray-500"
            )
    _sheet_import_block(is_admin)


@notify_errors
async def _sync_sheet(team_id: int, direction: str) -> None:
    """Sync on demand, the way the home-page section's Fetch now works.

    The actor is re-derived server-side inside sync_team, so a tab left open
    across a demotion stops syncing.
    """
    async with action_session() as (_session, actor):
        user_id = actor.user.id
    ui.notify("Syncing with Google Sheets…")
    outcome = await roster_sheets.sync_team(
        team_id, direction=direction, user_id=user_id
    )
    if outcome.failed:
        ui.notify(f"Sync failed: {outcome.message}", color="negative", multi_line=True)
    else:
        ui.notify(outcome.message, color="positive", multi_line=True)
    ui.navigate.to(f"/teams/{team_id}")


def _sheet_import_block(is_admin: bool) -> None:
    """The .csv import, moved here from the retired /import page.

    Deliberately still importer.run_import, unchanged: it scopes rows to the
    teams the actor manages all by itself, so a leader uploading here cannot
    reach anybody else's roster, and the dry-run -> preview -> apply flow is
    the one already covered by tests.
    """
    state: dict = {"content": None, "filename": None}
    ui.label("Import a .csv").classes("text-md font-medium mt-2")
    ui.label(
        "1. DO NOT edit the ID Column. "
        "2. Imports never delete anything and a blank cell never clears a "
        "field; they only add and update. "
        "3. Ensure import is congruent with provided template; "
        "system will not accept any errors."
        + (
            ""
            if is_admin
            else " Rows are limited to the teams you lead; new volunteers must "
            "be put on one of your teams in the same file."
        )
    ).classes("text-sm text-gray-500 vdb-prose")

    report_area = ui.column().classes("w-full gap-2")

    async def render_report(report: importer.ImportReport) -> None:
        report_area.clear()
        with report_area:
            if report.applied:
                ui.label("Import applied ✔").classes(
                    "text-positive text-lg font-medium"
                )
            elif report.has_errors:
                ui.label("Not applied — fix the errors below and re-upload.").classes(
                    "text-negative font-medium"
                )
            else:
                ui.label("Dry run — nothing written yet.").classes(
                    "text-amber-700 font-medium"
                )
            reactivated = (
                f", {report.volunteers_reactivated} reactivated"
                if report.volunteers_reactivated
                else ""
            )
            ui.label(
                f"volunteers: +{report.volunteers_created} new, "
                f"{report.volunteers_updated} updated{reactivated} · "
                f"memberships: +{report.memberships_created} new, "
                f"{report.memberships_updated} updated"
            )
            if report.warnings:
                count = len(report.warnings)
                # Warnings never block an import, so the ones that flag
                # possible duplicates or a suspect ID are easy to scroll
                # past. Put the count where the eye already is.
                ui.label(
                    f"⚠️ {count} warning{'' if count == 1 else 's'} — these do not "
                    "stop the import. Possible duplicates and suspect IDs all "
                    "appear here."
                ).classes("text-amber-700 font-medium")
            for issue in report.errors:
                ui.label(f"❌ {issue.sheet} row {issue.row}: {issue.message}").classes(
                    "text-negative text-sm"
                )
            for issue in report.warnings:
                ui.label(f"⚠️ {issue.sheet} row {issue.row}: {issue.message}").classes(
                    "text-amber-700 text-sm"
                )
            if not report.applied and not report.has_errors and state["content"]:
                ui.button(
                    "Apply this import", icon="publish", on_click=apply_import
                ).props("color=positive")

    @notify_errors
    async def on_upload(e: events.UploadEventArguments) -> None:
        state["content"] = await e.file.read()
        state["filename"] = e.file.name
        async with action_session() as (_, actor):
            user_id = actor.user.id  # run_import checks the right itself
        report = await importer.run_import(
            state["content"], dry_run=True, user_id=user_id
        )
        await render_report(report)

    @notify_errors
    async def apply_import() -> None:
        async with action_session() as (_, actor):
            user_id = actor.user.id  # run_import checks the right itself
        report = await importer.run_import(
            state["content"], dry_run=False, user_id=user_id
        )
        await render_report(report)
        if report.applied:
            ui.notify(f"Imported {state['filename']}", color="positive")

    ui.upload(
        label="Drop a .csv file here (validated before anything is written)",
        on_upload=on_upload,
        auto_upload=True,
        max_file_size=10_000_000,
    ).props('accept=".csv"').classes("w-full")


_IMPORT_ROWS = "Import its rows into the database"
_OVERWRITE = "Overwrite it from the database"


def _roster_sheet_dialog(team_id: int, linked: bool) -> None:
    """Link the team to a roster spreadsheet. Leaders/seconds and admins —
    enforced server-side on save."""
    with ui.dialog() as dialog, ui.card().classes("w-[32rem] gap-3"):
        ui.label("Roster spreadsheet").classes("text-lg font-medium")
        ui.label(
            "Paste the link of a Google Sheet shared as “anyone with the link "
            "can edit” — copy the roster template to make one."
        ).classes("text-sm text-gray-500")
        ui.label(
            "Keep this link private. It holds every member's email, phone and "
            "notes, and anyone who has it can change them. Share it only with "
            "the people who help run this team."
        ).classes("text-sm text-negative vdb-prose")
        url = (
            ui.input("Google Sheets link")
            .props("outlined dense maxlength=500")
            .classes("w-full")
        )
        direction = ui.radio([_OVERWRITE, _IMPORT_ROWS], value=_OVERWRITE).props(
            "dense"
        )
        direction.tooltip(
            "Overwriting keeps the parish database as it is and rewrites the "
            "sheet from it. Importing adds and updates from the sheet's rows "
            "— it never removes anybody."
        )
        ui.label(
            "Saving syncs straight away, so you will know at once whether the "
            "sheet is shared and shaped correctly."
        ).classes("text-sm text-gray-500")

        @notify_errors
        async def save() -> None:
            async with action_session() as (session, actor):
                (
                    await team_service.set_roster_sheet(
                        session, actor, team_id, url.value or ""
                    )
                ).unwrap()
                user_id = actor.user.id
            dialog.close()
            ui.notify("Syncing with Google Sheets…")
            outcome = await roster_sheets.sync_team(
                team_id,
                direction=(
                    roster_sheets.IMPORT
                    if direction.value == _IMPORT_ROWS
                    else roster_sheets.EXPORT
                ),
                user_id=user_id,
            )
            if outcome.failed:
                ui.notify(
                    f"Linked, but the first sync failed: {outcome.message}",
                    color="negative",
                    multi_line=True,
                )
            else:
                ui.notify(outcome.message, color="positive", multi_line=True)
            ui.navigate.to(f"/teams/{team_id}")

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=save)
    dialog.open()


def _home_doc_dialog(team_id: int, current: str | None) -> None:
    """Set or clear the home-page doc. Leader/second/core/admin — enforced
    server-side on save."""
    with ui.dialog() as dialog, ui.card().classes("w-[30rem] gap-3"):
        ui.label("Team home page doc").classes("text-lg font-medium")
        ui.label(
            "Paste the link of a Google Doc shared as “anyone with the link can "
            "view”. Its content is published on the public ministries index and "
            "refreshed nightly."
        ).classes("text-sm text-gray-500")
        url = (
            ui.input("Google Doc link", value=current or "")
            .props("outlined dense")
            .classes("w-full")
        )

        @notify_errors
        async def save(new_value: str | None) -> None:
            async with action_session() as (session, actor):
                await page_service.set_home_doc_url(session, actor, team_id, new_value)
            dialog.close()
            ui.navigate.to(f"/teams/{team_id}")

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            if current:
                ui.button("Clear", on_click=lambda: save(None)).props(
                    "flat color=negative"
                )
            ui.button("Save", on_click=lambda: save(url.value))
    dialog.open()


@notify_errors
async def _fetch_home_page(team_id: int) -> None:
    async with action_session() as (session, actor):
        team = await team_service.get(session, team_id)
        if team is None or not team.home_doc_url:
            raise LookupError("this team has no home page doc")
        async with httpx.AsyncClient() as client:
            # force: a human clicking "Fetch now" means really refetch — also
            # the repair path when image rows were damaged out-of-band
            page = await page_service.fetch_and_store(
                session, team, client, force=True, actor=actor
            )
    if page.status == "ok":
        ui.notify("Home page updated", color="positive")
    else:
        ui.notify(f"Fetch failed: {page.error}", color="negative")
    ui.navigate.to(f"/teams/{team_id}")


@ui.page("/teams/{team_id}")
async def team_detail(request: Request, team_id: int, as_of: str = ""):
    at = parse_as_of(as_of)
    base_url = str(request.base_url).rstrip("/")
    async with page_session() as (session, actor):
        # out of the tree rather than team_service.get(): the page reads the whole
        # table either way, and get() is a second round trip for a row in hand
        tree = await team_service.tree(session, at=at)
        team = tree.by_id.get(team_id)
        if team is None:
            with frame(
                "Team not found", actor, as_of=at, asof_path=f"/teams/{team_id}"
            ):
                ui.label(f"No team with id {team_id} at this time.")
            return
        paths = tree.paths
        slug = page_service.slug_map(paths).get(team_id)
        can_names = actor.can_view_roster_names(team_id)
        can_full = actor.can_view_full_roster(team_id)
        can_manage = actor.can_manage_team(team_id) and at is None
        # leader/second/core of this team may invite its members; never off a
        # snapshot, where the roster is history and the addresses may be stale
        can_invite = can_full and at is None
        roster = (
            (await team_service.roster(session, actor, team_id, at=at)).unwrap()
            if can_names
            else []
        )
        # accounts are not system-versioned (like photos): an as-of roster still
        # reports who can sign in *now*
        accounts = await user_service.accounts_by_volunteer(
            session, [v.id for _, v in roster]
        )
        children = tree.by_parent.get(team_id, [])
        volunteer_options = (
            await volunteer_service.name_map(session) if can_manage else {}
        )
        team_page = (
            await session.get(TeamPage, team_id) if can_full and at is None else None
        )
        # whose roster you are on has nothing to do with a page the world can
        # read; the check never pulls the html the way team_page does
        has_public_page = slug is not None and await page_service.is_published(
            session, team_id
        )
        team_sheet = await session.get(TeamSheet, team_id) if can_manage else None
        anniversaries = (
            await volunteer_service.team_anniversaries(
                session, team_id, elections_service.local_today()
            )
            if can_manage
            else []
        )
        upcoming_events = (
            await event_service.list_events(
                session, actor, team_id=team_id, from_=datetime.now(UTC)
            )
            if can_names and at is None
            else []
        )
    panel = VolunteerPanel(as_of, base_url)
    with frame(
        paths.get(team_id, team.name), actor, as_of=at, asof_path=f"/teams/{team_id}"
    ):
        if team.description:
            ui.label(team.description).classes("text-gray-600")
        if not team.is_active:
            ui.badge("inactive", color="muted")

        with ui.row().classes("gap-2 w-full items-center"):
            if actor.is_admin and at is None:
                options = _parent_options(tree, team_id)
                ui.button(
                    "Edit team",
                    icon="edit",
                    on_click=lambda: _team_dialog(options, team),
                ).props("dense outline")
                ui.button(
                    "Delete", icon="delete", on_click=lambda: _delete_team(team_id)
                ).props("dense outline color=negative")
            if can_full:
                # a link to a route (ui/team_files_route.py); the exporter
                # re-checks the actor there, so a tab left open across a
                # demotion stops exporting just as the handler did
                roster_suffix = f"?as_of={as_of}" if as_of else ""
                ui.button("Export roster (.csv)", icon="download").props(
                    f'dense outline href="/teams/{team_id}/roster.csv{roster_suffix}"'
                )

                # roster emails are already shown to can_full viewers, so the
                # buttons add convenience, not exposure; live view only — no
                # copying a historical snapshot's stale addresses
                emails = sorted({v.email for _, v in roster if v.email})
                if emails and at is None:
                    joined = ", ".join(emails)

                    def copy_emails(text: str = joined, n: int = len(emails)) -> None:
                        ui.clipboard.write(text)
                        ui.notify(f"{n} addresses copied", color="positive")

                    ui.button(
                        "Copy email list", icon="content_copy", on_click=copy_emails
                    ).props("dense outline")
                    ui.button("Email all (BCC)", icon="mail").props(
                        f'dense outline href="mailto:?bcc={quote(",".join(emails))}"'
                    ).tooltip(
                        "Opens your mail app with everyone in BCC; for very "
                        "large teams use Copy email list instead"
                    )
            # can_full viewers reach the page from the Volunteer home page
            # section below, so this is the door for everyone else — a reader
            # not on this team gets an otherwise empty row and this one link
            if has_public_page and not (can_full and at is None):
                ui.space()
                ui.button("View public homepage", icon="public").props(
                    f'dense outline href="/ministries/{slug}.html"'
                )

        if anniversaries:
            summary = "; ".join(
                f"{a.volunteer.full_name}: {a.years} "
                f"{'year' if a.years == 1 else 'years'} on {a.anniversary:%B %-d}"
                for a in anniversaries
            )
            with ui.row().classes("w-full bg-amber-100 rounded p-2 items-center gap-2"):
                ui.icon("celebration")
                ui.label(f"Service anniversaries — {summary}").classes(
                    "text-amber-900 font-medium"
                )
            ui.label(
                "Continuous service on this team, measured from the database's "
                "records — members imported when VolunteerDB was set up count "
                "from that import."
            ).classes("text-xs text-gray-500 vdb-prose")

        # core members included on purpose: leaders are often elderly and a
        # public page nobody can refresh goes stale (api/teams.py:set_home_doc)
        if can_full and at is None:
            _home_page_section(team, team_page, team_id, slug, base_url)

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
                        await membership_service.assign(
                            session, actor, who.value, team_id, TeamRole(role.value)
                        )
                    ui.navigate.reload()

                ui.button("Add", icon="person_add", on_click=add).props("dense")

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
                    volunteer_link(
                        volunteer.full_name, volunteer.id, panel, classes="w-48"
                    )
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
                    # every member sees this, not just full-roster viewers; the
                    # invite control rides along for leaders/seconds/core only
                    roster_account(
                        accounts.get(volunteer.id),
                        action=(
                            partial(
                                invites.invite_control,
                                volunteer.id,
                                volunteer.full_name,
                                volunteer.email,
                                accounts.get(volunteer.id),
                                base_url,
                                reveal=actor.is_admin,
                            )
                            if can_invite and volunteer.is_active
                            else None
                        ),
                    )
                    if can_manage:
                        ui.button(
                            icon="person_remove",
                            on_click=notify_errors(
                                lambda _, mid=membership.id: _remove_member(mid)
                            ),
                        ).props("dense flat color=negative").tooltip("Remove from team")

        if can_manage:
            _sheet_section(team_sheet, team_id, actor.is_admin)

        if children:
            ui.label("Sub-teams").classes("text-lg font-medium")
            with ui.row().classes("gap-2"):
                for child in children:
                    ui.button(child.name).props(
                        f'outline dense href="/teams/{child.id}"'
                    )

        if upcoming_events:
            ui.label("Upcoming events").classes("text-lg font-medium")
            with ui.column().classes("w-full gap-1"):
                for s in upcoming_events[:5]:
                    with ui.row().classes(
                        "w-full items-center gap-2 p-2 rounded bg-gray-50"
                    ):
                        ui.link(s.event.title, f"/events/{s.event.id}").classes(
                            "font-medium"
                        )
                        ui.label(
                            mail.event_when(s.event.starts_at, s.event.ends_at)
                        ).classes("text-sm text-gray-600")
                        ui.space()
                        cap = "∞" if s.capacity is None else s.capacity
                        ui.label(f"{s.filled}/{cap} filled").classes(
                            "text-sm text-gray-600"
                        )
                if len(upcoming_events) > 5:
                    ui.link(
                        f"All {len(upcoming_events)} upcoming events", "/events"
                    ).classes("text-sm")


async def _change_role(membership_id: int, role_value: str) -> None:
    async with action_session() as (session, actor):
        await membership_service.set_role(
            session, actor, membership_id, TeamRole(role_value)
        )
    ui.notify("Role updated", color="positive")


async def _remove_member(membership_id: int) -> None:
    async with action_session() as (session, actor):
        await membership_service.remove(session, actor, membership_id)
    ui.navigate.reload()


@notify_errors
async def _delete_team(team_id: int) -> None:
    async with action_session() as (session, actor):
        (await team_service.delete(session, actor, team_id)).unwrap()
    ui.navigate.to("/teams")
