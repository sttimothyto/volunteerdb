from contextlib import contextmanager
from datetime import datetime

from nicegui import ui

from ..env import current as current_env
from ..permissions import Actor
from ..services import mail_quota
from .a11y import heading, icon_button
from .context import asof_banner, asof_picker, clear_session
from .logo_dialog import site_logo
from .photo_dialog import photo_avatar
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
    the picker navigates back to) and the as_of they were rendered at.

    Every framed page gets the whole window: one width for the whole app, so
    moving between pages never shifts where the content starts. Running text
    keeps its own measure instead (.vdb-prose in theme.css) — that is what the
    old page-wide cap was really protecting, and it does not need the layout to
    shrink around it."""
    dark = apply_theme()
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
        ui.link("Dash", "/").classes("text-lg vdb-brand vdb-quiet")
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
                # open-in-new-tab work, left click still navigates in place
                ui.button(label).props(f'flat color=white dense href="{target}"')
        with (
            ui.button(icon="menu")
            .props('flat color=white dense round aria-label="Menu"')
            .classes("lt-md")
        ):
            with ui.menu():
                for label, target in nav_items:
                    ui.menu_item(label).props(f'href="{target}"')
        ui.space()
        _own_email(actor)
        _own_avatar(actor)
        _settings_menu(dark, as_of, asof_path)
        icon_button("logout", "Sign out", on_click=_logout).props(
            "flat color=white dense"
        )
    # p-4 keeps a gutter and lines the content up with the header's own px-4
    # instead of running into the window edge
    # the skip link's target. Not a <main>: NiceGUI's page container already
    # is one, and a second main landmark is a finding of its own
    with (
        ui.element("div").props('id="main" tabindex="-1"').classes("w-full"),
        ui.column().classes("w-full p-4 gap-4"),
    ):
        heading(title).classes("text-2xl vdb-page-title")
        if as_of is not None and asof_path is not None:
            asof_banner(as_of, asof_path)
        _mail_quota_banner(actor)
        yield


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
        box, ink, faint, icon = (
            "bg-red-100",
            "text-red-900",
            "text-red-800",
            "mark_email_unread",
        )
        headline = "Email sending is over its limit"
    else:
        box, ink, faint, icon = (
            "bg-amber-100",
            "text-amber-900",
            "text-amber-800",
            "outgoing_mail",
        )
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
            ).classes(f"text-xs {faint}")


def _own_email(actor: Actor) -> None:
    """Your address, and — for an account linked to a volunteer record — the
    way to your own profile.

    Unlinked accounts (the sync bot, an admin nobody linked) keep a plain
    label: there is no page to send them to. Same null check _own_avatar makes
    below, for the same reason. vdb-quiet keeps the header's own face — an
    anchor here should read as the address it already was."""
    classes = "text-sm gt-sm"  # at 80% opacity the address read 3.7:1
    if actor.volunteer_id is None:
        ui.label(actor.user.email).classes(classes).mark("header-email")
        return
    ui.link(actor.user.email, f"/volunteers/{actor.volunteer_id}").classes(
        f"{classes} vdb-quiet"
    ).tooltip("My volunteer profile").mark("header-email")


def _own_avatar(actor: Actor) -> None:
    """Your own headshot beside your address, clickable to change it.

    The same dialog the volunteer profile opens, so there is one upload
    workflow and one legal declaration. Nothing renders for an account with no
    volunteer record (the sync bot, an admin nobody linked): there is no row a
    photo could hang off. Unlike the address to its left this shows at every
    width — on a phone it is the only thing identifying who is signed in."""
    if actor.volunteer_id is None:
        return

    async def changed() -> None:
        ui.navigate.reload()

    # a plain flex div, not ui.row(): row() would claim the header's width
    with ui.element("div").classes("flex items-center mx-2"):
        photo_avatar(
            actor.volunteer_id,
            actor.volunteer_name or actor.user.email,
            actor.photo_at,
            on_change=changed,
            marker="header-avatar",
        )


def _settings_menu(
    dark: ui.dark_mode, as_of: datetime | None, asof_path: str | None
) -> None:
    """Everything that changes how you're reading the app, under one gear:
    dark mode, the manual, and (where the page supports it) the as-of date."""
    with ui.button(icon="settings").props(
        f'flat dense round color={"warning" if as_of else "white"} aria-label="Settings"'
    ):
        # to the left: the menu drops straight down over anything below the gear
        ui.tooltip("Settings").props('anchor="center left" self="center right"')
        with ui.menu(), ui.column().classes("p-3 gap-3 w-64"):
            ui.switch("Dark mode").bind_value(dark, "value").props("dense")
            ui.button("Password & sign-in", icon="key").props(
                'flat dense no-caps align=left href="/account"'
            ).classes("w-full")
            ui.button("Manual", icon="menu_book").props(
                'flat dense no-caps align=left href="/manual" target="_blank"'
            ).classes("w-full")
            if asof_path is not None:
                ui.separator()
                asof_picker(as_of, asof_path)


def _logout() -> None:
    clear_session()
    ui.navigate.to("/login")
