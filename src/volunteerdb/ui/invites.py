"""Sending an account-creation link, and the dialogs around it.

Two callers with different rights share this: the admin accounts page, which
mints invites for anybody, and the team roster / volunteer profile, where a
ministry leader, second, or core member invites somebody on their own team.
Both end in the same place — an armed ``/invite/<token>`` link, emailed if
outbound mail is configured, and shown on screen either way so it can be handed
over in person when the email does not arrive.

``base_url`` is threaded in rather than read from a global: it comes off the live
request (``str(request.base_url)``), which is what makes the link work behind the
reverse proxy, and the same idiom is used by ``_sub_request_dialog`` in
events_page.
"""

from collections.abc import Awaitable, Callable

from nicegui import ui

from ..config import settings
from ..models import AppUser
from ..permissions import require, volunteer_team_ids
from ..services import mail
from ..services import users as user_service
from .context import action_session, notify_errors


def invite_url(base_url: str, token: str) -> str:
    return f"{base_url}/invite/{token}"


async def email_invite(base_url: str, address: str, token: str) -> bool:
    """Mail the link. False when the send failed — never raises, so a dead mail
    provider still leaves the caller with a link to hand out."""
    return await mail.send_email(
        address, *mail.invite_email(invite_url(base_url, token))
    )


def show_invite(
    base_url: str,
    token: str,
    email: str,
    sent: bool | None = None,
    *,
    on_resend: Callable[[], Awaitable[None]] | None = None,
    reload_on_close: bool = False,
) -> None:
    """The link, its window, and how delivery went. `sent` None means nothing
    was emailed and the link is being handed out. `on_resend`, when given, adds
    a button to arm and mail a fresh one.

    `reload_on_close` refreshes the page once the reader is done with the link —
    the row behind this dialog still says "no account" and wants redrawing, but
    reloading any earlier would tear the link away before it was copied.
    """
    # "Send again" opens its own dialog on top of this one; reloading out from
    # under that would throw away the link it just produced.
    resent = {"yes": False}
    with ui.dialog() as dialog, ui.card().classes("gap-2 w-[34rem]"):
        ui.label(f"Invite link for {email}").classes("font-medium")
        url = invite_url(base_url, token)
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
                f"Invite email sent to {email}. Backup link above.",
                "text-gray-500",
            )
        else:
            note, color = (
                "Couldn't send the invite email — hand this link out instead.",
                "text-negative",
            )
        ui.label(note).classes(f"text-sm {color}")
        with ui.row().classes("justify-end w-full gap-2"):
            ui.button(
                "Copy",
                on_click=lambda: (ui.clipboard.write(url), ui.notify("Copied")),
            ).props("dense")
            if on_resend is not None:

                async def resend() -> None:
                    resent["yes"] = True
                    dialog.close()
                    await on_resend()

                ui.button("Send again", icon="mail", on_click=resend).props(
                    "dense outline"
                )
            ui.button("Close", on_click=dialog.close).props("flat dense")

    if reload_on_close:
        dialog.on("hide", lambda _: None if resent["yes"] else ui.navigate.reload())
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
    volunteer_id: int, name: str, email: str, base_url: str, *, again: bool = False
) -> None:
    """Confirm, create-or-rearm, mail, then show the link.

    Re-checks the permission inside its own session: the rendered control is a
    hint, never the gate."""
    if not await confirm_send(name, email, again=again):
        return
    async with action_session() as (session, actor):
        team_ids = await volunteer_team_ids(session, volunteer_id)
        require(actor.can_invite_volunteer(team_ids), "invite this volunteer")
        account, token = await user_service.invite_volunteer(session, volunteer_id)
        addr = account.email
    # Mail goes out after the commit: a send that fails must not roll the
    # account back, since the link on screen is still usable by hand.
    sent = await email_invite(base_url, addr, token)
    ui.notify(
        f"Invite emailed to {addr}" if sent else f"Invite created for {addr}",
        color="positive" if sent else "warning",
    )
    show_invite(base_url, token, addr, sent, reload_on_close=True)


def invite_control(
    volunteer_id: int,
    name: str,
    email: str | None,
    account: AppUser | None,
    base_url: str,
    *,
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
        ui.badge("no account", color="grey").props("outline").tooltip(
            "No email address on file — add one before they can be invited."
        )
        return

    pending = account is not None and user_service.invite_live(account)
    mark = f"invite-{where}-{volunteer_id}"

    @notify_errors
    async def go() -> None:
        await send_invite(volunteer_id, name, address, base_url, again=pending)

    if pending and account is not None:
        # Same affordance as the admin page's clickable "invite pending" badge:
        # reopening the dialog is how the link gets handed over in person.
        token = account.invite_token or ""
        until = account.invite_expires_at

        def reopen() -> None:
            show_invite(base_url, token, address, True, on_resend=go)

        ui.badge("invite sent", color="warning").classes("cursor-pointer").mark(
            mark
        ).on("click", lambda _: reopen()).tooltip(
            f"Invite link usable until {until:%Y-%m-%d %H:%M} — "
            "click for the link, or to send it again"
            if until
            else "Invite link outstanding — click for the link"
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
            ui.badge(idle_text, color="grey").props("outline")
            ui.icon("mail").classes("vdb-invite-hint")
        ui.label(action_text).classes("vdb-invite-action text-xs")
