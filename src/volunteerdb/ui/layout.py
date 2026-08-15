from contextlib import contextmanager
from datetime import datetime

from nicegui import ui

from ..permissions import Actor
from .context import asof_banner, asof_picker, clear_session
from .theme import apply_theme


@contextmanager
def frame(
    title: str,
    actor: Actor,
    *,
    as_of: datetime | None = None,
    asof_path: str | None = None,
):
    """Header + page column. Pages that can time-travel pass asof_path (the URL
    the picker navigates back to) and the as_of they were rendered at."""
    dark = apply_theme()
    nav_items = [("Teams", "/teams"), ("Volunteers", "/volunteers")]
    if actor.can_access_planning:
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
        _settings_menu(dark, as_of, asof_path)
        ui.button(icon="logout", on_click=_logout).props(
            "flat color=white dense"
        ).tooltip("Sign out")
    with ui.column().classes("w-full max-w-5xl mx-auto p-4 gap-4"):
        ui.label(title).classes("text-2xl vdb-page-title")
        if as_of is not None and asof_path is not None:
            asof_banner(as_of, asof_path)
        yield


def _settings_menu(
    dark: ui.dark_mode, as_of: datetime | None, asof_path: str | None
) -> None:
    """Everything that changes how you're reading the app, under one gear:
    dark mode, the manual, and (where the page supports it) the as-of date."""
    with ui.button(icon="settings").props(
        f"flat dense round color={'warning' if as_of else 'white'}"
    ):
        # to the left: the menu drops straight down over anything below the gear
        ui.tooltip("Settings").props('anchor="center left" self="center right"')
        with ui.menu(), ui.column().classes("p-3 gap-3 w-64"):
            ui.switch("Dark mode").bind_value(dark, "value").props("dense")
            ui.button(
                "Password & sign-in",
                icon="key",
                on_click=lambda: ui.navigate.to("/account"),
            ).props("flat dense no-caps align=left").classes("w-full")
            ui.button(
                "Manual",
                icon="menu_book",
                on_click=lambda: ui.navigate.to("/manual", new_tab=True),
            ).props("flat dense no-caps align=left").classes("w-full")
            if asof_path is not None:
                ui.separator()
                asof_picker(as_of, asof_path)


def _logout() -> None:
    clear_session()
    ui.navigate.to("/login")
