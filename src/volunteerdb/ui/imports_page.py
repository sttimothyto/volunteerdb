from nicegui import events, ui

from ..permissions import require
from ..sheets import exporter, importer
from .context import action_session, notify_errors, page_session
from .layout import frame


@ui.page("/import")
async def import_page():
    async with page_session() as (session, actor):
        if not actor.is_admin:
            with frame("Import/Export", actor):
                ui.label("Only admins can import or export the whole parish.").classes(
                    "text-gray-500"
                )
            return

    state: dict = {"content": None, "filename": None}

    with frame("Import / Export", actor):
        ui.label("Export").classes("text-lg font-medium")
        with ui.row().classes("gap-2"):

            @notify_errors
            async def download_template() -> None:
                ui.download(exporter.template_workbook(), "volunteerdb-template.xlsx")

            @notify_errors
            async def download_parish() -> None:
                async with action_session() as (session, actor):
                    require(actor.is_admin, "export the whole parish")
                    content = await exporter.export_workbook(session)
                ui.download(content, "volunteerdb-parish.xlsx")

            ui.button("Empty template", icon="description", on_click=download_template).props(
                "outline dense"
            )
            ui.button("Full parish export", icon="download", on_click=download_parish).props(
                "dense"
            )
        ui.label(
            "The export round-trips: edit it in a spreadsheet program and import it below."
        ).classes("text-sm text-gray-500")

        ui.separator()
        ui.label("Import").classes("text-lg font-medium")
        ui.label(
            "Two sheets: Volunteers (matched by email, then name) and Memberships "
            "(volunteer + team path + role). Imports never delete anything; "
            "they only add and update. All-or-nothing on errors."
        ).classes("text-sm text-gray-500")

        report_area = ui.column().classes("w-full gap-2")

        async def render_report(report: importer.ImportReport) -> None:
            report_area.clear()
            with report_area:
                if report.applied:
                    ui.label("Import applied ✔").classes("text-positive text-lg font-medium")
                elif report.has_errors:
                    ui.label("Not applied — fix the errors below and re-upload.").classes(
                        "text-negative font-medium"
                    )
                else:
                    ui.label("Dry run — nothing written yet.").classes("text-amber-700 font-medium")
                ui.label(
                    f"volunteers: +{report.volunteers_created} new, {report.volunteers_updated} updated · "
                    f"memberships: +{report.memberships_created} new, {report.memberships_updated} updated"
                )
                for issue in report.errors:
                    ui.label(f"❌ {issue.sheet} row {issue.row}: {issue.message}").classes(
                        "text-negative text-sm"
                    )
                for issue in report.warnings:
                    ui.label(f"⚠️ {issue.sheet} row {issue.row}: {issue.message}").classes(
                        "text-amber-700 text-sm"
                    )
                if not report.applied and not report.has_errors and state["content"]:
                    ui.button("Apply this import", icon="publish", on_click=apply_import).props(
                        "color=positive"
                    )

        @notify_errors
        async def on_upload(e: events.UploadEventArguments) -> None:
            state["content"] = await e.file.read()
            state["filename"] = e.file.name
            async with action_session() as (_, actor):
                require(actor.is_admin, "import spreadsheets")
                user_id = actor.user.id
            report = await importer.run_import(state["content"], dry_run=True, user_id=user_id)
            await render_report(report)

        @notify_errors
        async def apply_import() -> None:
            async with action_session() as (_, actor):
                require(actor.is_admin, "import spreadsheets")
                user_id = actor.user.id
            report = await importer.run_import(state["content"], dry_run=False, user_id=user_id)
            await render_report(report)
            if report.applied:
                ui.notify(f"Imported {state['filename']}", color="positive")

        ui.upload(
            label="Drop a .xlsx file here (validated before anything is written)",
            on_upload=on_upload,
            auto_upload=True,
            max_file_size=10_000_000,
        ).props('accept=".xlsx"').classes("w-full")
