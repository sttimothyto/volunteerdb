from contextlib import contextmanager

from nicegui import app, ui

from ..permissions import Actor


@contextmanager
def frame(title: str, actor: Actor):
    with ui.header().classes("items-center bg-primary text-white px-4"):
        ui.label("St. Timothy VolunteerDB").classes("text-lg font-bold cursor-pointer").on(
            "click", lambda: ui.navigate.to("/")
        )
        ui.space()
        ui.button("Dashboard", on_click=lambda: ui.navigate.to("/")).props("flat color=white dense")
        ui.button("Teams", on_click=lambda: ui.navigate.to("/teams")).props("flat color=white dense")
        ui.button("Volunteers", on_click=lambda: ui.navigate.to("/volunteers")).props(
            "flat color=white dense"
        )
        ui.button("Graph", on_click=lambda: ui.navigate.to("/graph")).props("flat color=white dense")
        if actor.is_admin:
            ui.button("Import/Export", on_click=lambda: ui.navigate.to("/import")).props(
                "flat color=white dense"
            )
            ui.button("Accounts", on_click=lambda: ui.navigate.to("/admin/users")).props(
                "flat color=white dense"
            )
        ui.space()
        ui.label(actor.user.email).classes("text-sm opacity-80")
        ui.button(icon="logout", on_click=_logout).props("flat color=white dense").tooltip(
            "Sign out"
        )
    with ui.column().classes("w-full max-w-5xl mx-auto p-4 gap-4"):
        ui.label(title).classes("text-2xl font-semibold")
        yield


def _logout() -> None:
    app.storage.user.clear()
    ui.navigate.to("/login")
