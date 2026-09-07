from decimal import Decimal
from functools import partial
from urllib.parse import quote
from zoneinfo import ZoneInfo

from nicegui import events, ui

from .. import query_lang, timefmt
from ..env import current as current_env
from ..errors import not_found
from ..fp import Err, Ok
from ..models import ROLE_LABELS, Membership, Team, TeamRole, TeamSheet, Volunteer
from ..services import events as event_service
from ..services import memberships as membership_service
from ..services import pages as page_service
from ..services import readmodels, roster_sheets
from ..services import reports as report_service
from ..services import teams as team_service
from ..services import volunteers as volunteer_service
from ..services.readmodels import TeamRoom
from ..sheets import importer
from ..sheets.common import sheet_url
from . import column_order, invites
from .account_status import roster_account
from .asof import parse_as_of
from .context import PageCtx, flash, page_ctx, run_command, success, toast
from .forms import WIDE, actions, confirm, dialog_card, required, valid
from .layout import frame
from .tables import count_text, wire_search
from .volunteer_panel import VolunteerPanel, volunteer_link
from .widgets import ROLE_OPTIONS, busy, inactive_badge, role_badge


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


def _filtered_rows(rows: list[dict], pred) -> list[dict]:
    """The rows a query predicate keeps, plus each one's ancestors."""
    return _with_ancestors(rows, {i for i, r in enumerate(rows) if pred(r)})


@ui.page("/teams")
async def teams_page(as_of: str = ""):
    at = parse_as_of(as_of, current_env().tz)
    async with page_ctx() as ctx:
        session, actor = ctx.session, ctx.actor
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
        count = ui.label(count_text(len(rows), None, "team")).classes(
            "text-sm text-gray-500"
        )
        if search is not None:
            wire_search(
                search,
                count,
                table,
                rows,
                noun="team",
                compile=query_lang.compile_teams,
                text_filter=_matching_rows,
                query_filter=_filtered_rows,
            )


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
    with dialog_card("Edit team" if team else "New team") as dialog:
        name = (
            required(ui.input("Name", value=team.name if team else ""))
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

        async def save() -> None:
            if not valid(name):
                return
            parent_id = parent.value or None
            weight_value = (
                Decimal(str(weight.value)) if weight.value is not None else None
            )

            async def command(ctx: PageCtx):
                if team is None:
                    return await team_service.create(
                        ctx.session,
                        ctx.actor,
                        name.value,
                        parent_id,
                        description.value or None,
                        workload_weight=weight_value,
                    )
                return await team_service.update(
                    ctx.session,
                    ctx.actor,
                    team.id,
                    name=name.value,
                    parent_team_id=parent_id,
                    description=description.value or None,
                    workload_weight=weight_value,
                    is_active=active.value if active is not None else None,
                )

            def done(saved, _effects, _report) -> None:
                dialog.close()
                flash("Team saved" if team is not None else "Team created")
                ui.navigate.to(f"/teams/{saved.id}")

            await run_command(command, on_ok=done, reload=False)

        actions(dialog, "Save", save)
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
            on_click=busy(lambda: _fetch_home_page(team_id)),
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
                on_click=busy(lambda: _sync_sheet(team_id, roster_sheets.IMPORT)),
            ).props("dense outline")
            ui.button(
                "Overwrite sheet",
                icon="upload",
                on_click=busy(lambda: _sync_sheet(team_id, roster_sheets.EXPORT)),
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
        template_url = current_env().settings.template_sheet_url
        if template_url:
            # The decorated Google Sheet (role dropdown, hidden ID column,
            # structure warning) replaces the bare CSV: copy it, share the
            # copy, link it here — the decoration comes along with the copy.
            ui.button("Roster template (Google Sheets)", icon="open_in_new").props(
                f'outline dense href="{template_url}" target="_blank"'
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


async def _sync_sheet(team_id: int, direction: str) -> None:
    """Sync on demand, the way the home-page section's Fetch now works.

    The actor is re-derived server-side inside sync_team, so a tab left open
    across a demotion stops syncing. sync_team is an orchestrator with units
    of work of its own, so it is not a command: the refusal is toasted here.
    """
    async with page_ctx() as ctx:
        user_id = ctx.actor.account.id
    env = ctx.env
    # no "Syncing…" toast: the button that was clicked shows it is working
    synced = await roster_sheets.sync_team(
        env, team_id, direction=direction, user_id=user_id, now=env.clock.now()
    )
    if isinstance(synced, Err):
        toast(synced.error)
        return
    outcome = synced.value
    if outcome.failed:
        flash(f"Sync failed: {outcome.message}", kind="negative", multi_line=True)
    else:
        flash(outcome.message, multi_line=True)
    ui.navigate.to(f"/teams/{team_id}")


def _sheet_import_block(is_admin: bool) -> None:
    """The .csv import, moved here from the retired /import page.

    Deliberately still importer.run_import, unchanged: it scopes rows to the
    teams the actor manages all by itself, so a leader uploading here cannot
    reach anybody else's roster, and the dry-run -> preview -> apply flow is
    the one already covered by tests.
    """
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

    async def render_report(
        report: importer.ImportReport, *, content: bytes, filename: str
    ) -> None:
        """The report, and -- for a clean dry run -- the Apply button with the
        very file it will apply captured, so nothing has to be remembered."""
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
            if not report.applied and not report.has_errors and content:
                ui.button(
                    "Apply this import",
                    icon="publish",
                    on_click=busy(lambda: apply_import(content, filename)),
                ).props("color=positive")

    async def _import(content: bytes, *, dry_run: bool):
        """run_import is an orchestrator with a unit of work of its own, so it
        is not a command; the refusal to import at all is toasted here."""
        async with page_ctx() as ctx:
            user_id = ctx.actor.account.id  # run_import checks the right itself
        report = await importer.run_import(
            ctx.env, content, dry_run=dry_run, user_id=user_id
        )
        if isinstance(report, Err):
            toast(report.error)
            return None
        return report.value

    async def on_upload(e: events.UploadEventArguments) -> None:
        content, filename = await e.file.read(), e.file.name
        report = await _import(content, dry_run=True)
        if report is not None:
            await render_report(report, content=content, filename=filename)

    async def apply_import(content: bytes, filename: str) -> None:
        report = await _import(content, dry_run=False)
        if report is None:
            return
        await render_report(report, content=content, filename=filename)
        if report.applied:
            success(f"Imported {filename}")

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
    with dialog_card("Roster spreadsheet", width=WIDE) as dialog:
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
            required(ui.input("Google Sheets link"))
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

        async def save() -> None:
            if not valid(url):
                return

            async def command(ctx: PageCtx):
                linked = await team_service.set_roster_sheet(
                    ctx.session, ctx.actor, team_id, url.value or ""
                )
                if isinstance(linked, Err):
                    return linked
                return Ok(ctx.actor.account.id)

            linked = await run_command(command, reload=False)
            if isinstance(linked, Err):
                return
            user_id = linked.value
            # the dialog stays open, its Save button working, until the
            # first sync has answered: that answer is what the reader waits for
            env = current_env()
            synced = await roster_sheets.sync_team(
                env,
                team_id,
                direction=(
                    roster_sheets.IMPORT
                    if direction.value == _IMPORT_ROWS
                    else roster_sheets.EXPORT
                ),
                user_id=user_id,
                now=env.clock.now(),
            )
            if isinstance(synced, Err):
                toast(synced.error)
                return
            outcome = synced.value
            if outcome.failed:
                flash(
                    f"Linked, but the first sync failed: {outcome.message}",
                    kind="negative",
                    multi_line=True,
                )
            else:
                flash(outcome.message, multi_line=True)
            dialog.close()
            ui.navigate.to(f"/teams/{team_id}")

        actions(dialog, "Save", save)
    dialog.open()


def _home_doc_dialog(team_id: int, current: str | None) -> None:
    """Set or clear the home-page doc. Leader/second/core/admin — enforced
    server-side on save."""
    with dialog_card("Team home page doc", width=WIDE) as dialog:
        ui.label(
            "Paste the link of a Google Doc shared as “anyone with the link can "
            "view”. Its content is published on the public ministries index and "
            "refreshed nightly."
        ).classes("text-sm text-gray-500")
        url = (
            required(ui.input("Google Doc link", value=current or ""))
            .props("outlined dense")
            .classes("w-full")
        )

        async def save(new_value: str | None) -> None:

            async def command(ctx: PageCtx):
                return await page_service.set_home_doc_url(
                    ctx.session, ctx.actor, team_id, new_value
                )

            def done(_value, _effects, _report) -> None:
                dialog.close()
                flash(
                    "Home page doc cleared"
                    if new_value is None
                    else "Home page doc saved"
                )
                ui.navigate.to(f"/teams/{team_id}")

            await run_command(command, on_ok=done, reload=False)

        def clear_button() -> None:
            if current:
                ui.button("Clear", on_click=lambda: save(None)).props(
                    "flat color=negative"
                )

        # Clear bypasses the rule on purpose: it is the way to a blank
        actions(
            dialog,
            "Save",
            lambda: save(url.value) if valid(url) else None,
            extra=clear_button,
        )
    dialog.open()


async def _fetch_home_page(team_id: int) -> None:
    async def command(ctx: PageCtx):
        team = await team_service.get(ctx.session, team_id)
        if team is None or not team.home_doc_url:
            return not_found("home page doc for this team")
        async with ctx.env.http.client() as client:
            # force: a human clicking "Fetch now" means really refetch — also
            # the repair path when image rows were damaged out-of-band
            return await page_service.fetch_and_store(
                ctx.session, team, client, force=True, actor=ctx.actor, now=ctx.now
            )

    def done(page, _effects, _report) -> None:
        if page.status == "ok":
            flash("Home page updated")
        else:
            flash(f"Fetch failed: {page.error}", kind="negative")
        ui.navigate.to(f"/teams/{team_id}")

    await run_command(command, on_ok=done, reload=False)


# --- the team page's sections --------------------------------------------------
#
# One function per block on /teams/{id}, in the order the page draws them.
# Each takes the room (readmodels.team_room) or the rows it draws; the
# handlers they drive are module-level and take ids.


def _copy_emails(emails: list[str]) -> None:
    ui.clipboard.write(", ".join(emails))
    success(f"{len(emails)} addresses copied")


def _team_actions(room: TeamRoom, *, is_admin: bool, as_of: str) -> None:
    """The row under the description: the admin's Edit and Delete, the
    exporter and the mail helpers for full-roster viewers, and for everyone
    else the one door to the public page."""
    team_id = room.team.id
    with ui.row().classes("gap-2 w-full items-center"):
        if is_admin and room.live:
            options = _parent_options(room.tree, team_id)
            ui.button(
                "Edit team",
                icon="edit",
                on_click=lambda: _team_dialog(options, room.team),
            ).props("dense outline")
            ui.button(
                "Delete",
                icon="delete",
                on_click=lambda: _delete_team(
                    team_id, room.team.name, places=len(room.roster)
                ),
            ).props("dense outline color=negative")
        if room.can_full:
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
            emails = room.emails
            if emails and room.live:
                ui.button(
                    "Copy email list",
                    icon="content_copy",
                    on_click=lambda: _copy_emails(emails),
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
        if room.has_public_page and not (room.can_full and room.live):
            ui.space()
            ui.button("View public homepage", icon="public").props(
                f'dense outline href="/ministries/{room.slug}.html"'
            )


def _anniversaries_banner(anniversaries: list[volunteer_service.Anniversary]) -> None:
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


def _subteams_row(children: list[Team]) -> None:
    ui.label("Sub-teams").classes("text-lg font-medium")
    with ui.row().classes("gap-2"):
        for child in children:
            ui.button(child.name).props(f'outline dense href="/teams/{child.id}"')


def _add_member_row(team_id: int, volunteer_options: dict[int, str]) -> None:
    ui.label("Add member").classes("text-lg font-medium")
    with ui.row().classes("items-center gap-2"):
        who = (
            required(ui.select(volunteer_options, label="Volunteer", with_input=True))
            .props("outlined dense")
            .classes("w-64")
        )
        role = (
            ui.select(ROLE_OPTIONS, label="Role", value=TeamRole.member.value)
            .props("outlined dense")
            .classes("w-52")
        )
        ui.button(
            "Add",
            icon="person_add",
            on_click=lambda: (
                _add_member(team_id, who.value, role.value) if valid(who) else None
            ),
        ).props("dense").mark("add-member")


def _roster_row(
    room: TeamRoom,
    membership: Membership,
    volunteer: Volunteer,
    panel: VolunteerPanel,
    base_url: str,
    *,
    reveal: bool,
) -> None:
    """One member: the name, the role (a picker for a manager), contact
    details for full-roster viewers, the sign-in status with the invite
    control for those who may send one, and the manager's Remove."""
    # the column widths live in theme.css (.vdb-roster-name,
    # .vdb-roster-email), which lets a phone drop them and stack
    # the row as a block under the name
    account = room.accounts.get(volunteer.id)
    with ui.row().classes("w-full items-center gap-3 p-2 rounded hover:bg-gray-100"):
        volunteer_link(
            volunteer.full_name, volunteer.id, panel, classes="vdb-roster-name"
        )
        if room.can_manage:
            role_select = ui.select(ROLE_OPTIONS, value=membership.role.value).props(
                "dense outlined"
            )
            role_select.on_value_change(
                lambda e, mid=membership.id: _change_role(mid, e.value)
            )
        else:
            role_badge(membership.role)
        if room.can_full:
            ui.label(volunteer.email or "").classes(
                "text-sm text-gray-600 vdb-roster-email"
            )
            ui.label(volunteer.phone or "").classes("text-sm text-gray-600")
        ui.space()
        # every member sees this, not just full-roster viewers; the
        # invite control rides along for leaders/seconds/core only
        roster_account(
            account,
            action=(
                partial(
                    invites.invite_control,
                    volunteer.id,
                    volunteer.full_name,
                    volunteer.email,
                    account,
                    base_url,
                    reveal=reveal,
                )
                if room.can_invite and volunteer.is_active
                else None
            ),
        )
        if room.can_manage:
            ui.button(
                icon="person_remove",
                on_click=lambda _, mid=membership.id, who=volunteer.full_name: (
                    _remove_member(mid, who, room.team.name)
                ),
            ).props("dense flat color=negative").mark(
                f"remove-member-{membership.id}"
            ).tooltip("Remove from team")


def _roster_section(
    room: TeamRoom, panel: VolunteerPanel, base_url: str, *, reveal: bool
) -> None:
    ui.label("Roster").classes("text-lg font-medium")
    if not room.can_names:
        ui.label(
            "You are not on this team, so its roster is not visible to you."
        ).classes("text-gray-500")
    elif not room.roster:
        ui.label("Nobody on this team yet.").classes("text-gray-500")
    for membership, volunteer in room.roster:
        _roster_row(room, membership, volunteer, panel, base_url, reveal=reveal)


def _upcoming_events_section(
    upcoming_events: list[event_service.EventSummary], tz: ZoneInfo
) -> None:
    ui.label("Upcoming events").classes("text-lg font-medium")
    with ui.column().classes("w-full gap-1"):
        for s in upcoming_events[:5]:
            with ui.row().classes("w-full items-center gap-2 p-2 rounded bg-gray-50"):
                ui.link(s.event.title, f"/events/{s.event.id}").classes("font-medium")
                ui.label(
                    timefmt.event_when(s.event.starts_at, s.event.ends_at, tz=tz)
                ).classes("text-sm text-gray-600")
                ui.space()
                cap = "∞" if s.capacity is None else s.capacity
                ui.label(f"{s.filled}/{cap} filled").classes("text-sm text-gray-600")
        if len(upcoming_events) > 5:
            ui.link(f"All {len(upcoming_events)} upcoming events", "/events").classes(
                "text-sm"
            )


@ui.page("/teams/{team_id}")
async def team_detail(team_id: int, as_of: str = ""):
    async with page_ctx() as ctx:
        actor, tz = ctx.actor, ctx.env.tz
        at = parse_as_of(as_of, tz)
        shown = await readmodels.team_room(
            ctx.session, actor, team_id, now=ctx.now, tz=tz, at=at
        )
    if isinstance(shown, Err):
        with frame("Team not found", actor, as_of=at, asof_path=f"/teams/{team_id}"):
            ui.label(f"No team with id {team_id} at this time.")
        return
    room = shown.value

    panel = VolunteerPanel(as_of, ctx.base_url)
    with frame(room.path, actor, as_of=at, asof_path=f"/teams/{team_id}"):
        if room.team.description:
            ui.label(room.team.description).classes("text-gray-600")
        if not room.team.is_active:
            inactive_badge()
        _team_actions(room, is_admin=actor.is_admin, as_of=as_of)
        if room.anniversaries:
            _anniversaries_banner(room.anniversaries)
        # core members included on purpose: leaders are often elderly and a
        # public page nobody can refresh goes stale (api/teams.py:set_home_doc)
        if room.can_full and room.live:
            _home_page_section(room.team, room.page, team_id, room.slug, ctx.base_url)
        if room.children:
            _subteams_row(room.children)
        if room.can_manage:
            _add_member_row(team_id, room.volunteer_options)
        _roster_section(room, panel, ctx.base_url, reveal=actor.is_admin)
        if room.can_manage:
            _sheet_section(room.sheet, team_id, actor.is_admin)
        if room.upcoming_events:
            _upcoming_events_section(room.upcoming_events, tz)


async def _add_member(team_id: int, volunteer_id: int | None, role_value: str) -> None:
    if not volunteer_id:  # the picker's own rule said so already (forms.valid)
        return

    async def command(ctx: PageCtx):
        return await membership_service.assign(
            ctx.session, ctx.actor, volunteer_id, team_id, TeamRole(role_value)
        )

    await run_command(command, reload=True, success="Added to the roster")


async def _change_role(membership_id: int, role_value: str) -> None:
    async def command(ctx: PageCtx):
        return await membership_service.set_role(
            ctx.session, ctx.actor, membership_id, TeamRole(role_value)
        )

    await run_command(command, reload=False, success="Role updated")


async def _remove_member(membership_id: int, name: str, team: str) -> None:
    """Take somebody off the roster, once the leader has said so twice: the
    icon is small, the row is one of sixty, and the wrong one is a phone
    call to make."""
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
        success=f"Removed from {team}",
    )


async def _delete_team(team_id: int, name: str, *, places: int) -> None:
    if places == 0:
        detail = "Nobody is on its roster. The history keeps the team."
    else:
        detail = (
            f"Its {places} roster place{'s' if places != 1 else ''} "
            f"go{'' if places != 1 else 'es'} with it. The history keeps them."
        )
    if not await confirm(
        f"Delete the team {name}?", detail=detail, yes="Delete the team", danger=True
    ):
        return

    async def command(ctx: PageCtx):
        return await team_service.delete(ctx.session, ctx.actor, team_id)

    def done(_value, _effects, _report) -> None:
        flash(f"Deleted the team {name}")
        ui.navigate.to("/teams")

    await run_command(command, on_ok=done, reload=False)
