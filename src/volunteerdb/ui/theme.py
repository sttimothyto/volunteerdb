"""Dark/light theme plumbing shared by framed pages and the login screens."""

from nicegui import app, ui

# The dark ground, for the moment before Quasar has classed <body>: the
# shared theme.css paints the page light, and a dark page would flash light
# without this. Per-client head html renders after the theme link.
_DARK_GROUND = "html{background-color:#1C1917;color-scheme:dark}"


def apply_theme() -> ui.dark_mode:
    """Call at the top of every page builder. Applies the persisted dark pref.

    The preference is None until the reader uses the switch: Quasar's auto
    mode, which follows the device. An explicit choice, either way, is kept
    (the switch writes True or False) and follows the browser across a sign
    out (main.py's session rotation, context.clear_session)."""
    app.storage.user.setdefault("dark_mode", None)
    pref = app.storage.user["dark_mode"]
    dark = ui.dark_mode(pref).bind_value(app.storage.user, "dark_mode")
    if pref is True:
        ui.add_head_html(f"<style>{_DARK_GROUND}</style>")
    elif pref is None:
        # the same anti-flash, on the device's say-so, as auto mode will decide
        ui.add_head_html(
            f"<style>@media (prefers-color-scheme: dark){{{_DARK_GROUND}}}</style>"
        )
    return dark
