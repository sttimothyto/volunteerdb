"""Sending an account-creation link, and the dialogs around it.

Two callers with different rights share this: the admin accounts page, which
mints invites for anybody, and the team roster / volunteer profile, where a
ministry leader, second, or core member invites somebody on their own team.
Both end in the same place — an armed ``/invite/<token>`` link, emailed to
the address on the volunteer's own record.

Only an **admin** is shown the link itself. Anyone holding it can redeem it and
sign in as that volunteer, and a leader may add any volunteer to their own team
and then edit their address — so displaying it to non-admins turned "invite
somebody on my team" into "take over any account that has never signed in".
Mailing it to the address on file keeps the workflow and puts the link where
only the volunteer can read it. Admins keep the visible copy: hand-delivery is
the answer when the mail bounces, and they can already do everything anyway.

``base_url`` is threaded in rather than read from a global: it comes off the live
request (``str(request.base_url)``), which is what makes the link work behind the
reverse proxy, and the same idiom is used by ``_sub_request_dialog`` in
events_page.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime

from nicegui import ui

from ..config import settings
from ..effects import delivered
from ..env import current as current_env
from ..errors import require
from ..models import AppUser
from ..permissions import volunteer_team_ids
from ..services import mail
from ..services import users as user_service
from .context import PageCtx, run_command


def invite_url(base_url: str, token: str) -> str:
    return f"{base_url}/invite/{token}"


def show_invite(
    base_url: str,
    token: str,
    email: str,
    sent: bool | None = None,
    *,
    reveal: bool = True,
    on_resend: Callable[[], Awaitable[None]] | None = None,
    reload_on_close: bool = False,
) -> None:
    """The link, its window, and how delivery went. `sent` None means nothing
    was emailed and the link is being handed out. `on_resend`, when given, adds
    a button to arm and mail a fresh one.

    `reveal` False keeps the token off the screen (see the module docstring):
    the reader learns that an invite is out and can send another, but the link
    itself only reaches the volunteer's mailbox.

    `reload_on_close` refreshes the page when the reader closes the link with
    the button — the row behind this dialog still says "no account" and wants
    redrawing, but reloading any earlier would tear the link away before it
    was copied. "Send again" closes this dialog without a reload and opens the
    fresh link's own, whose Close does it.
    """
    with ui.dialog() as dialog, ui.card().classes("gap-2 w-[34rem]"):
        ui.label(f"Invite link for {email}").classes("font-medium")
        url = invite_url(base_url, token)
        if reveal:
            ui.input(value=url).props("readonly outlined dense").classes("w-full")
        window = mail.ttl_window(settings().invite_ttl_hours)
        ui.label(
            f"Usable once, and only for the next {window}. After that "
            "they sign in with an emailed code and can set a password "
            "themselves — or you re-invite them."
        ).classes("text-sm text-gray-500")
        if sent is None:
            note, color = (
                "Hand this link to the volunteer (email, print, or in person).",
                "text-gray-500",
            )
        elif sent:
            note, color = (
                f"Invite email sent to {email}."
                + (" Backup link above." if reveal else ""),
                "text-gray-500",
            )
        elif reveal:
            note, color = (
                "Couldn't send the invite email — hand this link out instead.",
                "text-negative",
            )
        else:
            note, color = (
                f"Couldn't send the invite email to {email}. Check the address, "
                "then send again — or ask an admin for the link.",
                "text-negative",
            )
        ui.label(note).classes(f"text-sm {color}")
        with ui.row().classes("justify-end w-full gap-2"):
            if reveal:
                ui.button(
                    "Copy",
                    on_click=lambda: (ui.clipboard.write(url), ui.notify("Copied")),
                ).props("dense")
            if on_resend is not None:

                async def resend() -> None:
                    dialog.close()
                    await on_resend()

                ui.button("Send again", icon="mail", on_click=resend).props(
                    "dense outline"
                )

            def close() -> None:
                dialog.close()
                if reload_on_close:
                    ui.navigate.reload()

            ui.button("Close", on_click=close).props("flat dense")
    dialog.open()


def show_outstanding_invite(
    email: str,
    until: datetime | None,
    *,
    on_resend: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """An invite is already out — say so, and offer to replace it.

    Deliberately shows no link: only the digest is stored, so nobody (not even
    an admin) can recover one already sent. "Send again" mints a fresh link,
    mails it, and kills the previous one — which is the same trade every
    password-reset flow makes, and the reason this dialog exists at all."""
    with ui.dialog() as dialog, ui.card().classes("gap-2 w-[30rem]"):
        ui.label(f"An invite is already out to {email}").classes("font-medium")
        ui.label(
            f"It can be used until {until:%Y-%m-%d %H:%M}."
            if until
            else "It is still outstanding."
        ).classes("text-sm text-gray-500")
        ui.label(
            "The link itself is not kept — only a fingerprint of it, so a copy "
            "of the database is not a set of keys. To hand one over now, send a "
            "fresh link: the one already mailed stops working."
        ).classes("text-sm text-gray-500")
        with ui.row().classes("justify-end w-full gap-2"):
            if on_resend is not None:

                async def resend() -> None:
                    dialog.close()
                    await on_resend()

                ui.button("Send again", icon="mail", on_click=resend).props(
                    "dense outline"
                )
            ui.button("Close", on_click=dialog.close).props("flat dense")
    dialog.open()


async def confirm_send(name: str, email: str, *, again: bool = False) -> bool:
    """Ask before mailing a real person. The hover control that opens this is
    easy to hit by accident, and the email cannot be recalled."""
    verb = "Send another invite" if again else "Send an invite"
    with ui.dialog() as dialog, ui.card().classes("w-96 gap-3"):
        ui.label(f"{verb} to {name}?").classes("font-medium")
        ui.label(
            f"An account will be created for {email} and a one-time "
            "setup link emailed to that address."
            if not again
            else f"A fresh setup link will be emailed to {email}. "
            "The previous link stops working."
        ).classes("text-sm text-gray-500")
        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("Cancel", on_click=lambda: dialog.submit(False)).props(
                "flat"
            ).mark("invite-cancel")
            ui.button(verb, icon="mail", on_click=lambda: dialog.submit(True)).mark(
                "invite-confirm"
            )
    return bool(await dialog)


async def send_invite(
    volunteer_id: int,
    name: str,
    email: str,
    base_url: str,
    *,
    again: bool = False,
    reveal: bool = False,
) -> None:
    """Confirm, create-or-rearm, then show the link; the policy mails it.

    Re-checks the permission inside its own unit of work: the rendered
    control is a hint, never the gate."""
    if not await confirm_send(name, email, again=again):
        return

    async def command(ctx: PageCtx):
        team_ids = await volunteer_team_ids(ctx.session, volunteer_id)
        if denied := require(
            ctx.actor.can_invite_volunteer(team_ids), "invite this volunteer"
        ):
            return denied
        return await user_service.invite_volunteer(
            ctx.session, volunteer_id, invite=ctx.env.invite()
        )

    def done(value, effects, report) -> None:
        # The mail went out after the commit: a send that failed did not roll
        # the account back, since the link is still re-sendable (and, for an
        # admin, still on screen).
        account, token = value
        addr = account.email
        sent = delivered(effects, report)
        ui.notify(
            f"Invite emailed to {addr}" if sent else f"Invite created for {addr}",
            color="positive" if sent else "warning",
        )
        show_invite(base_url, token, addr, sent, reveal=reveal, reload_on_close=True)

    await run_command(command, on_ok=done, reload=False)


def invite_control(
    volunteer_id: int,
    name: str,
    email: str | None,
    account: AppUser | None,
    base_url: str,
    *,
    reveal: bool = False,
    where: str = "roster",
) -> None:
    """The roster/profile cell for somebody who could be invited.

    Reads as their sign-in status until pointed at, then offers the action —
    the status is what most viewers came for, the action is what a leader
    reaches for. A volunteer with no address on file cannot be invited at all,
    so they keep the plain badge and a note saying why.

    `where` only names the marker. /teams/{id} renders both the roster and the
    drawer, so one marker per volunteer would match twice and a test click would
    fire the flow twice over.
    """
    # An existing account's login IS its address, and it can differ from what the
    # volunteer record says after a relink — invite the address that will be used.
    address = (account.email if account is not None else email) or ""
    if not address:
        ui.badge("no account", color="muted").props("outline").tooltip(
            "No email address on file — add one before they can be invited."
        )
        return

    pending = account is not None and user_service.invite_live(
        account, now=current_env().clock.now()
    )
    mark = f"invite-{where}-{volunteer_id}"

    async def go() -> None:
        await send_invite(
            volunteer_id, name, address, base_url, again=pending, reveal=reveal
        )

    if pending and account is not None:
        # Same affordance as the admin page's clickable "invite pending" badge,
        # but it can only ever SEND — never re-display. Only the digest of the
        # link is stored (services.users._issue_invite), so an outstanding one
        # no longer exists in readable form anywhere; handing it over again
        # means minting a fresh one, which is what "Send again" does.
        until = account.invite_expires_at

        ui.badge("invite sent", color="warning").classes("cursor-pointer").mark(
            mark
        ).on(
            "click", lambda _: show_outstanding_invite(address, until, on_resend=go)
        ).tooltip(
            f"Invite link usable until {until:%Y-%m-%d %H:%M} — click to send it again"
            if until
            else "Invite link outstanding — click to send it again"
        )
        return

    again = account is not None
    idle_text = "invite expired" if again else "no account"
    action_text = "send a new invite" if again else "invite to create account"
    # Both faces sit in the DOM at all times, so a screen reader would otherwise
    # read the pair as one run-on phrase; the aria-label states the action alone.
    spoken = f"{action_text} — {name}".replace('"', "")
    with (
        ui.button(on_click=go)
        .props(f'flat dense no-caps aria-label="{spoken}"')
        .classes("vdb-invite-swap")
        .mark(mark)
        .tooltip(
            f"The old link has run out — email {address} a fresh one."
            if again
            else f"Email {address} a link to set up their account."
        )
    ):
        with ui.element("span").classes("vdb-invite-idle"):
            ui.badge(idle_text, color="muted").props("outline")
            ui.icon("mail").classes("vdb-invite-hint")
        ui.label(action_text).classes("vdb-invite-action text-xs")
