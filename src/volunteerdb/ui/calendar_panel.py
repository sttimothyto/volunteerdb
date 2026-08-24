"""The "Add to your calendar" button and the panel it opens.

The panel is a native popover: a <div popover> that a <button popovertarget>
toggles, with light dismiss and Escape handled by the browser. Nothing in it
needs the websocket — links, a read-only address to copy, a download, and a
<form> that POSTs the one action (rotating a personal address) — so it is
rendered as one block of HTML, and it keeps working on a page whose
connection has dropped. The button itself is a QBtn so it matches every
other button on the page; `popovertarget` falls through to its <button>.
"""

import html
from datetime import datetime

from nicegui import ui

from ..services import gcal


def _webcal(base_url: str, path: str) -> str:
    """The subscribe-me scheme Apple Calendar, Outlook and Thunderbird all
    open a subscription dialog for; the https address serves the same bytes."""
    return "webcal://" + base_url.split("://", 1)[-1] + path


def _address_field(label: str, url: str) -> str:
    return (
        f'<label class="vdb-feed-label">{label}'
        f'<input class="vdb-feed-url" readonly value="{html.escape(url, quote=True)}"></label>'
    )


def _when(iso: str | None) -> str:
    if not iso:
        return "not yet"
    try:
        return f"{datetime.fromisoformat(iso):%Y-%m-%d %H:%M} UTC"
    except ValueError:
        return iso


def _google_status(calendar: dict | None) -> str:
    """What an admin sees of the Google side: the calendar's own state and a
    way to open it. The sync job is the only writer, so there is no button."""
    if not gcal.enabled():
        return (
            '<p class="vdb-cal-admin"><strong>Google Calendar:</strong> the parish '
            "Google token is not configured, so no Google calendar is kept — see "
            '<a href="/manual/how-to/google-calendar-sync.html">the manual</a>.</p>'
        )
    if calendar is None:
        return (
            '<p class="vdb-cal-admin"><strong>Google Calendar:</strong> not created '
            "yet — the sync runs every 30 minutes and makes it on its first run.</p>"
        )
    cid = calendar["calendar_id"]
    return (
        '<p class="vdb-cal-admin"><strong>Google Calendar:</strong> created '
        f"{_when(calendar.get('created_at'))}; sharing last verified "
        f"{_when(calendar.get('verified_at'))}. "
        f'<a href="{html.escape(gcal.embed_url(cid), quote=True)}">Open in Google Calendar</a>'
        f' · <span class="vdb-feed-id">{html.escape(cid)}</span></p>'
    )


def subscribe_panel(
    *,
    view: str,
    base_url: str,
    token: str | None,
    calendar: dict | None,
    is_admin: bool,
) -> None:
    """The button and its popover for one view ("mine" or "parish")."""
    panel_id = f"vdb-subscribe-{view}"
    ui.button("Add to your calendar", icon="event_available").props(
        f'outline dense no-caps popovertarget="{panel_id}"'
    ).mark(f"subscribe-{view}")
    if view == "mine":
        feed = f"/calendar/mine/{token}.ics"
        body = (
            '<h3 id="{pid}-title">Your duties, in your own calendar</h3>'
            "<p>Subscribe once; every slot you sign up for appears in your phone "
            "or desktop calendar and stays up to date (calendars refresh about "
            "hourly).</p><ul>"
            f'<li><a href="{_webcal(base_url, feed)}">Subscribe in Apple Calendar, '
            "Outlook or Thunderbird</a></li>"
            "<li>Google Calendar: <em>Other calendars → + → From URL</em>, then paste "
            f"{_address_field('Feed address', base_url + feed)}</li>"
            '<li><a href="/calendar/mine.ics" download="my-duties.ics">Download a '
            ".ics file</a> — a one-time copy, not a subscription</li></ul>"
            '<p class="vdb-muted">The address is private to you: anyone holding it '
            "can read your duties. If it gets out, reset it and subscribe again.</p>"
            '<form method="post" action="/calendar/mine/reset" class="vdb-inline">'
            '<button type="submit" class="vdb-btn">Reset the address</button></form>'
        )
    else:
        feed = "/calendar/parish.ics"
        google = (
            f'<li><a href="{html.escape(gcal.public_url(calendar["calendar_id"]), quote=True)}">'
            "Add to Google Calendar</a></li>"
            if calendar
            else "<li>Google Calendar: <em>Other calendars → + → From URL</em>, then "
            f"paste {_address_field('Feed address', base_url + feed)}</li>"
        )
        body = (
            '<h3 id="{pid}-title">The whole parish, in your own calendar</h3>'
            "<p>Every team's events, kept up to date (calendars refresh about "
            "hourly).</p><ul>"
            f'<li><a href="{_webcal(base_url, feed)}">Subscribe in Apple Calendar, '
            "Outlook or Thunderbird</a></li>"
            f"{google}"
            '<li><a href="/calendar/parish.ics" download="parish-events.ics">Download '
            "a .ics file</a> — a one-time copy, not a subscription</li></ul>"
            + (_google_status(calendar) if is_admin else "")
        )
    ui.html(
        f'<div id="{panel_id}" popover="auto" class="vdb-popover" role="dialog" '
        f'aria-labelledby="{panel_id}-title">'
        + body.replace("{pid}", panel_id)
        + f'<button type="button" class="vdb-btn vdb-popover-close" '
        f'popovertarget="{panel_id}" popovertargetaction="hide">Close</button></div>',
        sanitize=False,
    ).mark(f"subscribe-panel-{view}")
