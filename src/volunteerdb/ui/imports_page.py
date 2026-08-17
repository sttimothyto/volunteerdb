from nicegui import events, ui

from ..config import settings
from ..permissions import require
from ..sheets import exporter, importer
from .context import action_session, notify_errors, page_session
from .layout import frame


@ui.page("/import")
async def import_page():
    async with page_session() as (session, actor):
        if not actor.can_import_export:
            with frame("Import/Export", actor):
                ui.label(
                    "Import/Export is available to admins and to team leaders/seconds."
                ).classes("text-gray-500")
            return

    state: dict = {"content": None, "filename": None}

    with frame("Import / Export", actor):
        ui.label("Export").classes("text-lg font-medium")

        @notify_errors
        async def download_template() -> None:
            ui.download(exporter.template_csv(), "volunteerdb-template.csv")

        @notify_errors
        async def download_data() -> None:
            """Parish for admins; the union of managed teams for leaders/seconds."""
            async with action_session() as (session, actor):
                if actor.is_admin:
                    scope, name = None, "parish"
                else:
                    require(bool(actor.managed_team_ids), "export your teams")
                    scope, name = actor.managed_team_ids, "my-teams"
                content = await exporter.export_csv(session, team_ids=scope)
            ui.download(content, f"volunteerdb-{name}.csv")

        data_label = "Full parish export" if actor.is_admin else "My teams export"
        with ui.row().classes("gap-2"):
            ui.button(data_label, icon="download", on_click=download_data).props(
                "dense"
            )
            if settings().template_sheet_url:
                # The decorated Google Sheet (role dropdown, hidden ID column,
                # structure warning) replaces the bare CSV: copy it, fill it in,
                # export as .csv, import below.
                ui.button("Roster template (Google Sheets)", icon="open_in_new").props(
                    f'outline dense href="{settings().template_sheet_url}" '
                    'target="_blank"'
                )
            else:  # dev fallback: no Drive template configured
                ui.button(
                    "Empty template", icon="description", on_click=download_template
                ).props("outline dense")
        ui.label(
            "The export round-trips: edit it in a spreadsheet program and import it below."
            + (
                ""
                if actor.is_admin
                else " Covers the teams you lead, including sub-teams."
            )
        ).classes("text-sm text-gray-500 vdb-prose")

        ui.separator()
        ui.label("Import").classes("text-lg font-medium")
        ui.label(
            "One .csv, one row per person per team: ID, name and contact columns, "
            "then Team and Role. The ID comes from exports and pins the row to that "
            "exact record — it is how you safely correct an email; leave it blank "
            "for new people (an ID that matches nobody is an error). A row with a "
            "blank Team just adds or updates the person. Rows without an ID are "
            "matched on their email alone — only a blank email cell matches by "
            "name, so a new address for someone already on file creates a second "
            "record. Imports never delete anything and a blank cell never clears a "
            "field; they only add and update. All-or-nothing on errors."
            + (
                ""
                if actor.is_admin
                else " Rows are limited to the teams you lead; new volunteers must be "
                "put on one of your teams in the same file."
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
                    ui.label(
                        "Not applied — fix the errors below and re-upload."
                    ).classes("text-negative font-medium")
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
                    f"memberships: +{report.memberships_created} new, {report.memberships_updated} updated"
                )
                if report.warnings:
                    count = len(report.warnings)
                    # Warnings never block an import, so the ones that flag
                    # possible duplicates or a suspect ID are easy to scroll
                    # past. Put the count where the eye already is.
                    ui.label(
                        f"⚠️ {count} warning{'' if count == 1 else 's'} — these do not stop "
                        "the import. Possible duplicates and suspect IDs all "
                        "appear here."
                    ).classes("text-amber-700 font-medium")
                for issue in report.errors:
                    ui.label(
                        f"❌ {issue.sheet} row {issue.row}: {issue.message}"
                    ).classes("text-negative text-sm")
                for issue in report.warnings:
                    ui.label(
                        f"⚠️ {issue.sheet} row {issue.row}: {issue.message}"
                    ).classes("text-amber-700 text-sm")
                if not report.applied and not report.has_errors and state["content"]:
                    ui.button(
                        "Apply this import", icon="publish", on_click=apply_import
                    ).props("color=positive")

        @notify_errors
        async def on_upload(e: events.UploadEventArguments) -> None:
            state["content"] = await e.file.read()
            state["filename"] = e.file.name
            async with action_session() as (_, actor):
                require(actor.can_import_export, "import spreadsheets")
                user_id = actor.user.id
            report = await importer.run_import(
                state["content"], dry_run=True, user_id=user_id
            )
            await render_report(report)

        @notify_errors
        async def apply_import() -> None:
            async with action_session() as (_, actor):
                require(actor.can_import_export, "import spreadsheets")
                user_id = actor.user.id
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
