"""Your own sign-in settings: set, change or drop the account's password.

This is the self-service half of "I forgot my password". NIST SP 800-63B
§4.1.2.1 draws the line: "Replacement of a forgotten password where the
subscriber can authenticate with one or more other authenticators is considered
to be the binding of a new authenticator... rather than account recovery."
Every account here has that second authenticator — the emailed one-time code —
so a volunteer who forgot their password signs in with a code and sets a new
one here, and nobody has to wait for an admin to cut a fresh invite link.

What that costs: a session that signed in *with the password* must re-type it
to change it (an unattended browser should not be able to lock its owner out),
while a session that signed in with an emailed code has already proved the
thing a reset link proves and is not asked again. Either way the account's
address gets a notification, which §4.1.2 requires to be independent of the
transaction that made the change.
"""

import structlog
from fastapi import Request
from nicegui import ui

from .. import passwords, throttle
from ..auth import async_verify_password
from ..log import audit_log
from ..services import mail
from ..services import users as user_service
from .context import action_session, notify_errors, page_session, session_auth_method
from .layout import frame
from .login import confirm_email_url

logger = structlog.get_logger(__name__)


@ui.page("/account")
async def account_page(request: Request):
    base_url = str(request.base_url).rstrip("/")
    login_url = f"{base_url}/login"
    account_url = f"{base_url}/account"
    async with page_session() as (session, actor):
        user = actor.user
        user_id = user.id
        stored_hash = user.password_hash
        email, has_password = user.email, stored_hash is not None
        pending = user.pending_email if user_service.email_change_live(user) else None
        pending_until = user.email_change_expires_at if pending else None
        # An emailed code (or a freshly redeemed invite) is possession of the
        # mailbox — the same proof a reset link carries, so it stands in for
        # the forgotten password.
        proved_by_email = session_auth_method() in ("otp", "invite")
        must_retype = has_password and not proved_by_email

    @notify_errors
    async def request_email_change() -> None:
        addr = (new_email.value or "").strip()
        if not addr:
            ui.notify("Type the new address first", color="warning")
            return
        # The budget is on *sends*, not failures: what is worth abusing here is
        # the parish's sender, one address at a time.
        key = f"email-change:{user_id}"
        if throttle.blocked(key, 5, 900):
            ui.notify(
                "Too many address changes requested — try again in a few minutes.",
                color="negative",
            )
            return
        async with action_session() as (session, actor):
            account, token = await user_service.start_email_change(
                session, actor.user.id, addr
            )
            target = account.pending_email
        throttle.hit(key)
        audit_log("auth.email_change_requested", user=f"{user_id}:{email}", to=target)
        # Both after the commit. The new address gets the proof — it is only a
        # claim until somebody reads mail there — and the old address gets a
        # warning it can still act on (§4.1.2: the notice travels a channel the
        # browser making the change cannot suppress).
        hours = int(user_service.EMAIL_CHANGE_TTL.total_seconds() // 3600)
        await mail.send_email(
            target,
            *mail.email_change_email(confirm_email_url(base_url, token), target, hours),
        )
        await mail.send_email(
            email, *mail.email_change_requested_email(target, account_url, hours)
        )
        ui.notify(
            f"Confirmation sent to {target}. Nothing changes until the link "
            "in it is opened.",
            color="positive",
            multi_line=True,
            timeout=8000,
        )
        ui.navigate.reload()

    @notify_errors
    async def drop_email_change() -> None:
        async with action_session() as (session, actor):
            await user_service.cancel_email_change(session, actor.user.id)
        audit_log("auth.email_change_cancelled", user=f"{user_id}:{email}")
        ui.notify("Address change cancelled — the link no longer works.")
        ui.navigate.reload()

    @notify_errors
    async def save() -> None:
        new, again = new_password.value or "", confirm.value or ""
        weak = passwords.problem(new, email=email)
        if weak:
            ui.notify(weak, color="negative", multi_line=True, timeout=8000)
            return
        if new != again:
            ui.notify("The two passwords don't match", color="negative")
            return
        if must_retype:
            # Failed attempts here count against the same budget as failed
            # sign-ins for this account (SP 800-63B §3.2.2).
            key = f"pw:{email.lower()}"
            if throttle.blocked(key, 5, 900):
                ui.notify(
                    "Too many failed attempts — try again in a few minutes.",
                    color="negative",
                )
                return
            if not await async_verify_password(stored_hash, current.value or ""):
                throttle.hit(key)
                logger.warning("auth.password_change_denied", email=email)
                ui.notify("That is not your current password", color="negative")
                return
        async with action_session() as (session, actor):
            await user_service.set_password(session, actor.user.id, new)
        audit_log("auth.password_set", user=f"{user.id}:{email}", via="self-service")
        await mail.send_email(email, *mail.password_changed_email(login_url))
        ui.notify(
            "Password saved. You can sign in with it from now on.", color="positive"
        )
        ui.navigate.reload()

    @notify_errors
    async def remove() -> None:
        with ui.dialog() as dialog, ui.card().classes("w-96 gap-3"):
            ui.label(
                "Remove the password from this account? You'll sign in by "
                "entering your email and typing the code we send you. Any API "
                "token you hold stops working."
            )
            with ui.row().classes("justify-end w-full gap-2"):
                ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("flat")
                ui.button(
                    "Remove password", on_click=lambda: dialog.submit(True)
                ).props("color=negative")
        if not await dialog:
            return
        async with action_session() as (session, actor):
            await user_service.clear_password(session, actor.user.id)
        audit_log("auth.password_cleared", user=f"{user.id}:{email}")
        await mail.send_email(
            email, *mail.password_changed_email(login_url, removed=True)
        )
        ui.notify("Password removed — you now sign in with emailed codes.")
        ui.navigate.reload()

    with frame("Your account", actor):
        with ui.card().classes("w-full max-w-xl gap-2"):
            ui.label(email).classes("font-medium")
            ui.label(
                "You sign in with your email address and a password."
                if has_password
                else "You sign in with a one-time code emailed to this address."
            ).classes("text-sm text-gray-500")
            if not has_password:
                ui.label(
                    "Setting a password is optional — the emailed code works "
                    "forever. It is only needed to use the JSON API."
                ).classes("text-sm text-gray-500")

        with ui.card().classes("w-full max-w-xl gap-3"):
            ui.label("Change your email address")
            ui.label(
                "This is the address you sign in at, and — for volunteers — "
                "the one on every ministry roster you serve on. Both move "
                "together, once the new address confirms itself."
            ).classes("text-sm text-gray-500")
            if pending is not None:
                with ui.row().classes("items-center gap-2 w-full no-wrap"):
                    ui.icon("mark_email_unread").classes("text-amber-700")
                    ui.label(
                        f"Waiting for {pending} to confirm"
                        + (
                            f" — the link stops working "
                            f"{pending_until.astimezone().strftime('%a %d %b, %H:%M')}"
                            if pending_until is not None
                            else ""
                        )
                    ).classes("text-sm text-amber-800")
                    ui.space()
                    ui.button("Cancel", on_click=drop_email_change).props(
                        "flat dense color=negative"
                    )
            new_email = (
                ui.input("New email address")
                .props("outlined dense autocomplete=email")
                .classes("w-full")
                .mark("new-email")
                .on("keydown.enter", request_email_change)
            )
            with ui.row().classes("w-full justify-end"):
                ui.button("Send confirmation", on_click=request_email_change).props(
                    "dense"
                )

        with ui.card().classes("w-full max-w-xl gap-3"):
            ui.label("Change your password" if has_password else "Set a password")
            if must_retype:
                current = (
                    ui.input("Current password", password=True)
                    .props("outlined dense autocomplete=current-password")
                    .classes("w-full")
                    .mark("current-password")
                )
            elif has_password:
                current = None
                ui.label(
                    "You signed in with an emailed code, so you can set a new "
                    "password without the old one."
                ).classes("text-sm text-gray-500")
            else:
                current = None
            new_password = (
                ui.input("New password", password=True, password_toggle_button=True)
                .props("outlined dense autocomplete=new-password")
                .classes("w-full")
                .mark("new-password")
            )
            ui.label(passwords.GUIDANCE).classes("text-xs text-gray-500")
            confirm = (
                ui.input(
                    "Repeat new password", password=True, password_toggle_button=True
                )
                .props("outlined dense autocomplete=new-password")
                .classes("w-full")
                .mark("repeat-password")
                .on("keydown.enter", save)
            )
            with ui.row().classes("w-full justify-between items-center"):
                if has_password:
                    ui.button("Remove password", on_click=remove).props(
                        "flat dense color=negative"
                    )
                else:
                    ui.space()
                ui.button("Save password", on_click=save).props("dense")
