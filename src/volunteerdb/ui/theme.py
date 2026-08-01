"""Dark/light theme plumbing shared by framed pages and the login screens."""

from nicegui import app, ui


def apply_theme() -> ui.dark_mode:
    """Call at the top of every page builder. Applies the persisted dark pref."""
    app.storage.user.setdefault("dark_mode", False)  # toggle() raises on None
    dark = ui.dark_mode().bind_value(app.storage.user, "dark_mode")
    if app.storage.user["dark_mode"]:
        # anti-flash: per-client head html renders after the shared theme.css link
        ui.add_head_html(
            "<style>html{background-color:#1C1917;color-scheme:dark}</style>"
        )
    return dark
