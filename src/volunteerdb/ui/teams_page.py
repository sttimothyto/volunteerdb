from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import quote

import httpx
from fastapi import Request
from nicegui import ui

from .. import query_lang
from ..models import ROLE_LABELS, TeamPage, TeamRole, TeamSheet
from ..services import elections as elections_service
from ..services import events as event_service
from ..services import interest as interest_service
from ..services import mail
from ..services import memberships as membership_service
from ..services import pages as page_service
from ..services import reports as report_service
from ..services import teams as team_service
from ..services import volunteers as volunteer_service
from ..sheets import exporter
from . import column_order
from .context import (
    action_session,
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
    for team in all_teams:
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
        all_teams = await team_service.list_all(session, at)
        show_coverage = actor.is_admin or bool(actor.managed_team_ids)
        coverage = (
            await report_service.coverage(session, at, teams=all_teams)
            if show_coverage
            else []
        )

    rows = _hierarchy_rows(all_teams, coverage, actor)

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
            # the search box grows into the free space and holds New team against
            # the right edge; with no teams to search there is nothing growing,
            # so the spacer takes over that job
            if search is None:
                ui.space()
            if actor.is_admin and at is None:
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
                <span v-if="props.row.depth" class="text-grey-5 q-mr-xs">└</span>
                <a :href="props.row.href" class="vdb-quiet" @click.stop>
                    {{ props.row.name }}
                </a>
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
        count = ui.label(_team_count(len(rows))).classes("text-sm text-gray-500")
        if search is not None:
            _wire_search(search, count, table, rows)


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
            ui.button(
                "Download QR Code to Public page",
                icon="qr_code_2",
                on_click=lambda: ui.download(
                    page_service.qr_png(f"{base_url}/ministries/{slug}.html"),
                    f"{slug}-qr.png",
                ),
            ).props("dense outline")
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


def _sheet_section(team_sheet: TeamSheet | None) -> None:
    """The team's Google Drive roster sheet, for leaders/seconds: the nightly
    sync applies sheet edits to the database and mirrors the database back."""
    ui.label("Roster spreadsheet").classes("text-lg font-medium")
    if team_sheet is None or not team_sheet.file_id:
        ui.label(
            "The nightly sync (2:30) creates a Google Sheet for this team's "
            "roster; the link will appear here."
        ).classes("text-sm text-gray-500 vdb-prose")
        return
    with ui.row().classes("items-center gap-2"):
        ui.link(
            "Google Sheet",
            f"https://docs.google.com/spreadsheets/d/{team_sheet.file_id}",
            new_tab=True,
        )
        ui.label(
            "Edits sync into the database nightly (2:30); rows removed from "
            "the sheet leave the roster. The same sync grants this team's "
            "leaders and seconds edit access, at the email on their volunteer "
            "record — Google emails an invitation the first time."
        ).classes("text-sm text-gray-500 vdb-prose")
    if team_sheet.last_status == "error":
        ui.label(f"Last sync failed: {team_sheet.last_error}").classes(
            "text-negative text-sm"
        )
    elif team_sheet.last_synced_at is not None:
        ui.label(f"Last synced {team_sheet.last_synced_at:%Y-%m-%d %H:%M}").classes(
            "text-sm text-gray-500"
        )


def _application_form_section(team, team_id: int) -> None:
    """The team's own Google application form: anyone who expresses interest
    on the public ministry page is emailed it directly. Same audience as the
    home-page controls (leaders/seconds/core members and admins)."""
    ui.label("Application form").classes("text-lg font-medium")
    if not team.application_form_url:
        with ui.row().classes("items-center gap-2"):
            ui.button(
                "Set application form",
                icon="add_link",
                on_click=lambda: _application_form_dialog(team_id, None),
            ).props("dense outline")
            ui.label(
                "Link the team's Google Form — people who express interest on "
                "the public ministry page get it emailed automatically."
            ).classes("text-sm text-gray-500 vdb-prose")
        return
    with ui.row().classes("items-center gap-2"):
        ui.link("Google Form", team.application_form_url, new_tab=True)
        ui.button(
            "Change",
            icon="edit",
            on_click=lambda: _application_form_dialog(
                team_id, team.application_form_url
            ),
        ).props("dense flat")
        ui.label(
            "Emailed automatically to anyone who expresses interest on the "
            "public ministry page."
        ).classes("text-sm text-gray-500")


def _application_form_dialog(team_id: int, current: str | None) -> None:
    """Set or clear the application form. Leader/second/core/admin — enforced
    server-side on save."""
    with ui.dialog() as dialog, ui.card().classes("w-[30rem] gap-3"):
        ui.label("Team application form").classes("text-lg font-medium")
        ui.label(
            "Paste the team's Google Form link (docs.google.com/forms/… or "
            "forms.gle/…). People who express interest on the public ministry "
            "page receive it by email."
        ).classes("text-sm text-gray-500")
        url = (
            ui.input("Google Form link", value=current or "")
            .props("outlined dense")
            .classes("w-full")
        )

        @notify_errors
        async def save(new_value: str | None) -> None:
            async with action_session() as (session, actor):
                from ..permissions import require

                require(
                    actor.can_view_full_roster(team_id),
                    "manage this team's application form",
                )
                await team_service.set_application_form_url(session, team_id, new_value)
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


def _interests_section(interests) -> None:
    """Unresolved public-form submissions, for the team's managers. Resolve
    once handled (form returned, person contacted, or not a fit)."""
    ui.label("Interested people").classes("text-lg font-medium")
    ui.label(
        "From the public ministry page. Resolve an entry once you've followed up."
    ).classes("text-sm text-gray-500")
    for interest in interests:
        with ui.row().classes(
            "w-full items-center gap-3 p-2 rounded hover:bg-gray-100"
        ):
            ui.label(interest.name).classes("font-medium w-48")
            ui.label(interest.email).classes("text-sm text-gray-600 w-56")
            ui.label(interest.phone or "").classes("text-sm text-gray-600 w-36")
            ui.label(f"{interest.created_at:%Y-%m-%d}").classes("text-sm text-gray-500")
            ui.space()
            ui.button(
                icon="task_alt",
                on_click=notify_errors(
                    lambda _, iid=interest.id: _resolve_interest(iid)
                ),
            ).props("dense flat").tooltip("Resolve (handled)")
        if interest.note:
            ui.label(interest.note).classes("text-sm text-gray-600 pl-4 italic")


async def _resolve_interest(interest_id: int) -> None:
    async with action_session() as (session, actor):
        from ..permissions import require

        interest = await interest_service.get(session, interest_id)
        if interest is None:
            raise LookupError("interest vanished")
        require(actor.can_manage_team(interest.team_id), "manage this team's roster")
        await interest_service.resolve(session, interest_id, resolved_by=actor.user.id)
    ui.navigate.reload()


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
                from ..permissions import require

                require(
                    actor.can_view_full_roster(team_id),
                    "manage this team's home page",
                )
                await page_service.set_home_doc_url(session, team_id, new_value)
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
        from ..permissions import require

        require(actor.can_view_full_roster(team_id), "manage this team's home page")
        team = await team_service.get(session, team_id)
        if team is None or not team.home_doc_url:
            raise LookupError("this team has no home page doc")
        async with httpx.AsyncClient() as client:
            # force: a human clicking "Fetch now" means really refetch — also
            # the repair path when image rows were damaged out-of-band
            page = await page_service.fetch_and_store(session, team, client, force=True)
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
        team = await team_service.get(session, team_id, at=at)
        if team is None:
            with frame(
                "Team not found", actor, as_of=at, asof_path=f"/teams/{team_id}"
            ):
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
            await volunteer_service.name_map(session) if can_manage else {}
        )
        team_page = (
            await session.get(TeamPage, team_id) if can_full and at is None else None
        )
        team_sheet = await session.get(TeamSheet, team_id) if can_manage else None
        interests = (
            await interest_service.unresolved(session, team_id) if can_manage else []
        )
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
    slug = page_service.slug_map(paths).get(team_id)

    panel = VolunteerPanel(as_of)
    with frame(
        paths.get(team_id, team.name), actor, as_of=at, asof_path=f"/teams/{team_id}"
    ):
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
                csv_stem = team.name.lower().replace(" ", "-")

                async def export_roster() -> None:
                    async with action_session() as (session, _):
                        content = await exporter.export_csv(
                            session, team_id=team_id, at=at
                        )
                    ui.download(content, f"{csv_stem}.csv")

                ui.button(
                    "Export roster (.csv)", icon="download", on_click=export_roster
                ).props("dense outline")

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

        if can_full and at is None:
            _home_page_section(team, team_page, team_id, slug, base_url)
            _application_form_section(team, team_id)
        if can_manage:
            _sheet_section(team_sheet)
            if interests:
                _interests_section(interests)

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
