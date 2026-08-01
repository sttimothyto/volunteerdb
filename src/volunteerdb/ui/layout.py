from contextlib import contextmanager

from nicegui import ui

from ..permissions import Actor
from .context import clear_session
from .theme import apply_theme


@contextmanager
def frame(title: str, actor: Actor):
    dark = apply_theme()
    nav_items = [("Teams", "/teams"), ("Volunteers", "/volunteers")]
    if actor.is_admin or actor.managed_team_ids:
        nav_items.append(("Planning", "/planning"))
    if actor.can_import_export:
        nav_items.append(("Import/Export", "/import"))
    if actor.is_admin:
        nav_items += [
            ("Accounts", "/admin/users"),
            ("Fields", "/admin/fields"),
            ("Workload", "/admin/workload"),
        ]
    with ui.header().classes("items-center text-white px-4 vdb-header"):
        ui.label("VDB").classes("text-lg cursor-pointer vdb-brand").on(
            "click", lambda: ui.navigate.to("/")
        )
        ui.space()
        # Full button row on wide screens, a single menu button below 1024px.
        # Use Quasar's gt-sm/lt-md helpers, never Tailwind's `hidden md:flex`:
        # Quasar ships `.hidden{display:none!important}`, which beats Tailwind's
        # plain `display:flex` and hides the row at every width.
        with ui.row().classes("items-center gap-0 gt-sm"):
            for label, target in nav_items:
                ui.button(label, on_click=lambda t=target: ui.navigate.to(t)).props(
                    "flat color=white dense"
                )
        with (
            ui.button(icon="menu")
            .props("flat color=white dense round")
            .classes("lt-md")
        ):
            with ui.menu():
                for label, target in nav_items:
                    ui.menu_item(label, on_click=lambda t=target: ui.navigate.to(t))
        ui.space()
        ui.label(actor.user.email).classes("text-sm opacity-80 gt-sm")
        ui.button(
            icon="menu_book", on_click=lambda: ui.navigate.to("/manual", new_tab=True)
        ).props("flat color=white dense round").tooltip("Manual")
        ui.button(icon="dark_mode", on_click=dark.toggle).props(
            "flat color=white dense round"
        ).bind_icon_from(
            dark, "value", lambda v: "light_mode" if v else "dark_mode"
        ).tooltip("Toggle dark mode")
        ui.button(icon="logout", on_click=_logout).props(
            "flat color=white dense"
        ).tooltip("Sign out")
    with ui.column().classes("w-full max-w-5xl mx-auto p-4 gap-4"):
        ui.label(title).classes("text-2xl vdb-page-title")
        yield


def _logout() -> None:
    clear_session()
    ui.navigate.to("/login")
