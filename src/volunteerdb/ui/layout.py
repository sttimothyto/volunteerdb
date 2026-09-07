from contextlib import contextmanager
from datetime import datetime

from nicegui import context, ui

from ..env import current as current_env
from ..permissions import Actor
from ..services import mail_quota
from ..services import photos as photo_service
from . import help_links
from .a11y import heading, icon_button
from .asof import as_of_query, asof_banner, asof_picker, with_as_of
from .context import clear_session, flash, show_flashed
from .logo_dialog import site_logo
from .photo_dialog import open_photo_dialog
from .theme import apply_theme


@contextmanager
def frame(
    title: str,
    actor: Actor,
    *,
    help: str | None = None,
    as_of: datetime | None = None,
    asof_path: str | None = None,
):
    """Header + page column. Pages that can time-travel pass asof_path (the URL
    the picker navigates back to) and the as_of they were rendered at.

    `help` names the manual page for this screen (help_links.PAGE_HELP): a
    small "?" at the end of the title row opens it in a new tab, so the
    guide is one click from the screen it describes rather than forty links
    at the foot of the dashboard.

    Every framed page gets the whole window: one width for the whole app, so
    moving between pages never shifts where the content starts. Running text
    keeps its own measure instead (.vdb-prose in theme.css) — that is what the
    old page-wide cap was really protecting, and it does not need the layout to
    shrink around it."""
    dark = apply_theme()
    # what the action before this page load wanted said, now that there is
    # a page to say it on
    show_flashed()
    here = _current_path()
    # while a snapshot is open, the links to the pages that can show it
    # carry the date; the others show today, and the banner says so
    snapshot = as_of_query(as_of) if as_of is not None else ""
    nav_items = [
        ("Teams", "/teams"),
        ("Volunteers", "/volunteers"),
        ("Events", "/events"),
    ]
    if actor.can_access_elections:
        nav_items.append(("Elections", "/elections"))
    if actor.is_admin:
        nav_items += [
            ("Accounts", "/admin/users"),
            ("Fields", "/admin/fields"),
            ("Workload", "/admin/workload"),
        ]
    with ui.header().classes("items-center text-white px-4 vdb-header"):
        # the first thing a keyboard reaches: past the header, into the page
        ui.html('<a class="vdb-skip" href="#main">Skip to content</a>', sanitize=False)
        # the parish's own mark, ahead of the brand word it belongs to;
        # clickable for an admin, which is how a logo gets replaced
        site_logo(actor, classes="h-8 w-auto mr-2")
        home = ui.link("Dashboard", with_as_of("/", snapshot)).classes(
            "text-lg vdb-brand vdb-quiet"
        )
        if here == "/":
            home.props('aria-current="page"')
        # the nav cluster sits against the brand, split from it by a double rule
        # echoing the header's own bottom border; only the spacer below the nav
        # is left, so the account controls still hold the right edge
        ui.element("div").classes("vdb-nav-rule")
        # Full button row on wide screens, a single menu button below 1024px.
        # Use Quasar's gt-sm/lt-md helpers, never Tailwind's `hidden md:flex`:
        # Quasar ships `.hidden{display:none!important}`, which beats Tailwind's
        # plain `display:flex` and hides the row at every width.
        with (
            ui.element("nav")
            .props('aria-label="Main"')
            .classes("flex items-center gap-0 gt-sm")
        ):
            for label, target in nav_items:
                # href renders the QBtn as a real <a>: right-click / middle-click
                # open-in-new-tab work, left click still navigates in place.
                # aria-current names the page the reader is on; theme.css
                # underlines it in the accent (2.4.8, and the eye's own map)
                ui.button(label).props(
                    f'flat color=white dense href="{with_as_of(target, snapshot)}"'
                    + _current(here, target)
                )
        with (
            ui.button(icon="menu")
            .props('flat color=white dense round aria-label="Menu"')
            .classes("lt-md")
        ):
            with ui.menu():
                for label, target in nav_items:
                    ui.menu_item(label).props(
                        f'href="{with_as_of(target, snapshot)}"'
                        + _current(here, target)
                    )
        ui.space()
        _account_menu(actor)
        _settings_menu(dark, as_of, asof_path)
    # p-4 keeps a gutter and lines the content up with the header's own px-4
    # instead of running into the window edge; on a phone theme.css narrows
    # both (.vdb-page, .vdb-header) so a 360px screen keeps a 336px column.
    # the skip link's target. Not a <main>: NiceGUI's page container already
    # is one, and a second main landmark is a finding of its own
    with (
        ui.element("div").props('id="main" tabindex="-1"').classes("w-full"),
        ui.column().classes("w-full p-4 gap-4 vdb-page"),
    ):
        with ui.row().classes("items-center gap-2 w-full no-wrap"):
            heading(title).classes("vdb-page-title")
            if help is not None:
                icon_button("help_outline", "Help for this page").props(
                    f'flat dense round href="{help_links.page_help_href(help)}" '
                    'target="_blank" rel="noopener"'
                ).classes("vdb-help").mark("page-help")
        if as_of is not None and asof_path is not None:
            asof_banner(as_of, asof_path)
        _mail_quota_banner(actor)
        yield


def _current_path() -> str:
    """The path this page was asked for -- what the header marks as "you
    are here". Empty outside a request (a background task): nothing marked."""
    try:
        return context.client.request.url.path
    except Exception:
        return ""


def _current(here: str, target: str) -> str:
    """The aria-current prop when `here` is `target` or a page under it:
    /teams/15 is the Teams page's, /admin/users is Accounts'."""
    if here == target or here.startswith(target.rstrip("/") + "/"):
        return ' aria-current="page"'
    return ""


def _mail_quota_banner(actor: Actor) -> None:
    """The "this instance is running out of email" strip — admins only, and
    only when the counters say so.

    The mail provider allows 200 messages a day and 1,000 a month, and the app
    finds out it has spent them by a send simply failing: a sign-in code that
    never arrives, an event cancellation nobody reads. Nothing in the app can
    buy more, which is exactly why the banner exists and why it names a person
    rather than offering a button — the fix is a bigger plan or less sending,
    and both belong to whoever set the instance up.

    `Actor.mail_quota` is None for everybody else and for an instance that is
    comfortably inside its allowance (actors.load_actor), so this draws
    nothing at all on a normal page for a normal user. Non-admins are not shown
    it deliberately: a volunteer can neither raise the plan nor stop the
    nightly digests, and a warning you cannot act on is just noise on the page
    you came to read.
    """
    quota = actor.mail_quota
    if quota is None or not actor.is_admin:  # belt and braces: the gate is here too
        return
    critical = quota.level == "critical"
    # Literal class strings per branch, never f-string interpolation into a
    # Tailwind name: a class assembled at runtime is invisible to any tool that
    # scans the source for the classes to keep. asof_banner beside this does
    # the same.
    if critical:
        box, ink, icon = "vdb-crit-box", "vdb-crit-ink", "mark_email_unread"
        headline = "Email sending is over its limit"
    else:
        box, ink, icon = "vdb-warn-box", "vdb-warn-ink", "outgoing_mail"
        headline = "Email sending is heading over its limit"
    contact = mail_quota.support_contact(current_env().settings)
    reach = (
        f" Contact the administrator who set up this website ({contact})."
        if contact
        else " Contact the administrator who set up this website."
    )
    with (
        ui.row()
        .classes(f"w-full {box} rounded p-2 items-start gap-2")
        .mark("mail-quota-banner")
    ):
        ui.icon(icon).classes(f"{ink} mt-1")
        with ui.column().classes("gap-0"):
            ui.label(headline).classes(f"{ink} font-medium")
            ui.label(
                f"This site can send {mail_quota.DAILY_CAP} emails a day and "
                f"{mail_quota.MONTHLY_CAP:,} a month — {quota.reason}. Past "
                "that, messages stop going out, including sign-in codes." + reach
            ).classes(f"text-sm {ink} vdb-prose")
            ui.label(
                f"So far: {quota.today:,} today, {quota.month_to_date:,} this "
                f"month (on course for about {quota.projected_month:,})."
            ).classes(f"text-xs {ink}")


def _account_menu(actor: Actor) -> None:
    """You, under one button: the headshot (or a person icon) and, on a wide
    screen, the address beside it. The menu holds your name, *My profile*
    and *Change photo* for an account linked to a volunteer record, *Your
    account*, and *Sign out*.

    One place for everything that is about the reader, where there were
    four: an address that was a link, a photo that was a button, a gear item
    called something else ("Password & sign-in"), and a sign-out icon. On a
    phone the address is inside the menu, so the guide's "open a team page,
    click your own name, then Full profile" is gone. An account with no
    volunteer record (the sync bot, an admin nobody linked) gets the icon
    and the two items that apply: there is no profile to send them to."""
    name = actor.volunteer_name or actor.account.email

    async def changed(message: str) -> None:
        flash(message)
        ui.navigate.reload()  # the header shows the new photo too

    with (
        ui.button()
        .props('flat dense no-caps color=white aria-label="Your account"')
        .classes("vdb-account")
        .mark("account-menu")
    ):
        # Quasar's own .flex wraps; no-wrap keeps the caret beside the face
        # when the header squeezes the button on a phone
        with ui.element("div").classes("flex no-wrap items-center gap-2"):
            _headshot(actor)
            # at 80% opacity the address read 3.7:1; the class keeps it plain
            ui.label(actor.account.email).classes("text-sm gt-sm").mark("header-email")
            ui.icon("arrow_drop_down")
        with ui.menu(), ui.column().classes("p-2 gap-0 w-64"):
            ui.label(name).classes("font-medium px-3 pt-1")
            if name != actor.account.email:
                ui.label(actor.account.email).classes("text-xs text-gray-500 px-3 pb-1")
            if actor.volunteer_id is not None:
                volunteer_id = actor.volunteer_id
                ui.button("My profile", icon="person").props(
                    f'flat dense no-caps align=left href="/volunteers/{volunteer_id}"'
                ).classes("w-full").mark("menu-profile")
                # the same dialog the profile opens: one upload workflow, one
                # legal declaration
                ui.button(
                    "Change photo",
                    icon="add_a_photo",
                    on_click=lambda: open_photo_dialog(
                        volunteer_id, name, actor.photo_at, changed
                    ),
                ).props("flat dense no-caps align=left").classes("w-full").mark(
                    "header-photo"
                )
            ui.button("Your account", icon="key").props(
                'flat dense no-caps align=left href="/account"'
            ).classes("w-full").mark("menu-account")
            ui.separator()
            ui.button("Sign out", icon="logout", on_click=_logout).props(
                "flat dense no-caps align=left"
            ).classes("w-full").mark("sign-out")


def _headshot(actor: Actor) -> None:
    """The menu button's face: the reader's photo, or the person icon."""
    if actor.volunteer_id is not None and actor.photo_at is not None:
        ui.image(photo_service.photo_url(actor.volunteer_id, actor.photo_at)).props(
            'loading="lazy"'
        ).classes("w-8 h-8 rounded-full object-cover").mark("header-avatar")
    else:
        ui.icon("person").classes("text-2xl").mark("header-avatar")


def _settings_menu(
    dark: ui.dark_mode, as_of: datetime | None, asof_path: str | None
) -> None:
    """Everything that changes how you're reading the app, under one gear:
    dark mode, the manual, and (where the page supports it) the as-of date.
    Your account is under the account menu beside it, not here."""
    with ui.button(icon="settings").props(
        f'flat dense round color={"warning" if as_of else "white"} aria-label="Settings"'
    ):
        # to the left: the menu drops straight down over anything below the gear
        ui.tooltip("Settings").props('anchor="center left" self="center right"')
        with ui.menu(), ui.column().classes("p-3 gap-3 w-64"):
            ui.switch("Dark mode").bind_value(dark, "value").props("dense")
            ui.button("Manual", icon="menu_book").props(
                'flat dense no-caps align=left href="/manual" target="_blank"'
            ).classes("w-full")
            if asof_path is not None:
                ui.separator()
                asof_picker(as_of, asof_path)


def _logout() -> None:
    clear_session()
    ui.navigate.to("/login")
