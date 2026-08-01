from contextlib import contextmanager

from nicegui import ui

from ..permissions import Actor
from .context import clear_session
from .theme import apply_theme


@contextmanager
def frame(title: str, actor: Actor):
    dark = apply_theme()
    with ui.header().classes("items-center text-white px-4 vdb-header"):
        ui.label("VDB").classes("text-lg cursor-pointer vdb-brand").on(
            "click", lambda: ui.navigate.to("/")
        )
        ui.space()
        ui.button("Teams", on_click=lambda: ui.navigate.to("/teams")).props("flat color=white dense")
        ui.button("Volunteers", on_click=lambda: ui.navigate.to("/volunteers")).props(
            "flat color=white dense"
        )
        ui.button("Graph", on_click=lambda: ui.navigate.to("/graph")).props("flat color=white dense")
        if actor.can_import_export:
            ui.button("Import/Export", on_click=lambda: ui.navigate.to("/import")).props(
                "flat color=white dense"
            )
        if actor.is_admin:
            ui.button("Accounts", on_click=lambda: ui.navigate.to("/admin/users")).props(
                "flat color=white dense"
            )
            ui.button("Fields", on_click=lambda: ui.navigate.to("/admin/fields")).props(
                "flat color=white dense"
            )
            ui.button("Capacity", on_click=lambda: ui.navigate.to("/admin/capacity")).props(
                "flat color=white dense"
            )
        ui.space()
        ui.label(actor.user.email).classes("text-sm opacity-80")
        ui.button(icon="menu_book", on_click=lambda: ui.navigate.to("/manual", new_tab=True)).props(
            "flat color=white dense round"
        ).tooltip("Manual")
        ui.button(icon="dark_mode", on_click=dark.toggle).props(
            "flat color=white dense round"
        ).bind_icon_from(dark, "value", lambda v: "light_mode" if v else "dark_mode").tooltip(
            "Toggle dark mode"
        )
        ui.button(icon="logout", on_click=_logout).props("flat color=white dense").tooltip(
            "Sign out"
        )
    with ui.column().classes("w-full max-w-5xl mx-auto p-4 gap-4"):
        ui.label(title).classes("text-2xl vdb-page-title")
        yield


def _logout() -> None:
    clear_session()
    ui.navigate.to("/login")
