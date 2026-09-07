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

The page is one card per thing about you -- the address you sign in at, your
photo, your calendar feed, the address change, the password -- one function
each; the work behind their buttons is module-level and takes the typed values
and the facts the page knew at load.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import structlog
from nicegui import ui

from .. import passwords, timefmt
from ..auth import async_verify_password
from ..domain import EmailChangeAttempted, SignInFailed
from ..env import current as current_env
from ..fp import expect
from ..services import photos as photo_service
from ..services import users as user_service
from .a11y import heading
from .calendar_panel import subscribe_panel
from .context import (
    PageCtx,
    fail,
    flash,
    page_ctx,
    perform,
    rate_limit,
    run_command,
    session_auth_method,
    toast,
    warn,
)
from .forms import confirm, required, valid
from .layout import frame
from .photo_dialog import open_photo_dialog

logger = structlog.get_logger(__name__)


# --- the work ------------------------------------------------------------------


async def _request_email_change(address: str, *, user_id: int, base_url: str) -> None:
    """Stage the new address and mail it a confirmation link."""
    addr = address.strip()
    if not addr:
        warn("Type the new address first")
        return
    # The budget is on *sends*, not failures: what is worth abusing here is
    # the parish's sender, one address at a time.
    now = current_env().clock.now()
    if denied := rate_limit(
        f"email-change:{user_id}", now=now, what="change your email address"
    ):
        toast(denied.error)
        return
    # charge every attempt, before the service can reveal whether the
    # address is taken: a failed probe must count too, or it is an
    # unthrottled account-existence oracle.
    await perform([EmailChangeAttempted(user_id)], base_url=base_url, now=now)

    async def command(ctx: PageCtx):
        return await user_service.start_email_change(
            ctx.session,
            ctx.actor.account.id,
            addr,
            now=ctx.now,
            token=ctx.env.rng.token(),
        )

    def done(value, _effects, _report) -> None:
        account, _token = value
        flash(
            f"Confirmation sent to {account.pending_email}. Nothing changes "
            "until the link in it is opened.",
            multi_line=True,
        )

    await run_command(command, on_ok=done)


async def _drop_email_change() -> None:
    async def command(ctx: PageCtx):
        return await user_service.cancel_email_change(ctx.session, ctx.actor.account.id)

    await run_command(
        command, success="Address change cancelled — the link no longer works."
    )


async def _save_password(
    new: str,
    again: str,
    current: str,
    *,
    email: str,
    stored_hash: str | None,
    must_retype: bool,
    ip: str,
    base_url: str,
) -> None:
    """Set the password, once it passes the policy and -- for a session that
    signed in with the old one -- once the old one is re-typed."""
    weak = passwords.problem(new, email=email, site_terms=current_env().password_terms)
    if weak:
        fail(weak, multi_line=True)
        return
    if new != again:
        fail("The two passwords don't match")
        return
    if must_retype:
        if stored_hash is None:
            # The page was drawn for an account that had a password, and an
            # admin's reissue_invite (services.users) has since cleared it:
            # the retyped one can prove nothing now, and the link they were
            # mailed is the way back in.
            fail(
                "Your password was reset. Use the link you were sent to set a new one."
            )
            return
        # Failed attempts here count against the same budgets as failed
        # sign-ins for this account (SP 800-63B §3.2.2): the per-account
        # bucket AND the per-IP flood bucket, exactly as the login page does.
        now = current_env().clock.now()
        if denied := rate_limit(
            f"pw:{email.lower()}",
            f"pw-ip:{ip}",
            now=now,
            what="confirm your current password",
        ):
            toast(denied.error)
            return
        if not await async_verify_password(stored_hash, current):
            logger.warning("auth.password_change_denied", email=email)
            await perform(
                [SignInFailed("password", email, ip)], base_url=base_url, now=now
            )
            fail("That is not your current password")
            return

    async def command(ctx: PageCtx):
        return await user_service.set_password(
            ctx.session, ctx.actor.account.id, new, site_terms=ctx.env.password_terms
        )

    await run_command(
        command, success="Password saved. You can sign in with it from now on."
    )


async def _photo_changed(message: str) -> None:
    """The photo dialog's outcome: said on the page that comes back, since
    the card, the header and the menu all show the picture."""
    flash(message)
    ui.navigate.reload()


async def _remove_password() -> None:
    if not await confirm(
        "Remove the password from this account? You'll sign in by "
        "entering your email and typing the code we send you. Any API "
        "token you hold stops working.",
        yes="Remove password",
        danger=True,
    ):
        return

    async def command(ctx: PageCtx):
        return await user_service.clear_password(ctx.session, ctx.actor.account.id)

    await run_command(
        command, success="Password removed — you now sign in with emailed codes."
    )


# --- the cards -----------------------------------------------------------------


def _signin_card(email: str, has_password: bool) -> None:
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


def _photo_card(volunteer_id: int, name: str, photo_at: datetime | None) -> None:
    """Your headshot and the button that changes it: the same dialog, with
    the same declaration, that the profile page and the header's menu open.
    Here too because this is the page a person comes to for what is theirs,
    and a menu item under a small picture is not where everyone looks."""
    with ui.card().classes("w-full max-w-xl gap-3"):
        heading("Your photo", level=2)
        ui.label(
            "Shown at the right of the header, on your profile, in the side "
            "panel that opens from a roster, and on the ministry graph. A "
            "headshot is best; it is stored as a 400×400 square."
        ).classes("text-sm text-gray-500")
        with ui.row().classes("items-center gap-4 no-wrap"):
            if photo_at is not None:
                ui.image(photo_service.photo_url(volunteer_id, photo_at)).props(
                    'loading="lazy"'
                ).classes("w-24 h-24 rounded-full object-cover").mark("account-photo")
            else:
                ui.icon("person").classes("text-6xl text-gray-400").mark(
                    "account-photo"
                )
            ui.button(
                "Change photo" if photo_at is not None else "Add a photo",
                icon="add_a_photo",
                on_click=lambda: open_photo_dialog(
                    volunteer_id, name, photo_at, _photo_changed
                ),
            ).props("dense outline").mark("account-photo-button")


def _calendar_card(base_url: str, feed_token: str) -> None:
    with ui.card().classes("w-full max-w-xl gap-2"):
        heading("Your duties in your own calendar", level=2)
        ui.label(
            "Subscribe your phone or desktop calendar to the events you are "
            "signed up for; it stays current as you sign up and withdraw. The "
            "same panel is on the Events page."
        ).classes("text-sm text-gray-500")
        subscribe_panel(
            view="mine",
            base_url=base_url,
            token=feed_token,
            calendar=None,
            is_admin=False,
        )


def _email_card(
    *,
    user_id: int,
    base_url: str,
    pending: str | None,
    pending_until: datetime | None,
    tz: ZoneInfo,
) -> None:
    """Change the sign-in address: the change waiting to be confirmed, if
    any, and the box for a new one."""
    with ui.card().classes("w-full max-w-xl gap-3"):
        heading("Change your email address", level=2)
        ui.label(
            "This is the address you sign in at, and — for volunteers — "
            "the one on every ministry roster you serve on. Both move "
            "together, once the new address confirms itself."
        ).classes("text-sm text-gray-500")
        if pending is not None:
            with ui.row().classes("items-center gap-2 w-full no-wrap"):
                ui.icon("mark_email_unread").classes("vdb-warn-ink")
                ui.label(
                    f"Waiting for {pending} to confirm"
                    + (
                        f" — the link stops working {timefmt.when_short(pending_until, tz)}"
                        if pending_until is not None
                        else ""
                    )
                ).classes("text-sm vdb-warn-ink")
                ui.space()
                ui.button("Cancel", on_click=_drop_email_change).props(
                    "flat dense color=negative"
                )

        async def request() -> None:
            if not valid(new_email):
                return
            await _request_email_change(
                new_email.value or "", user_id=user_id, base_url=base_url
            )

        new_email = (
            required(ui.input("New email address"))
            .props("outlined dense autocomplete=email")
            .classes("w-full")
            .mark("new-email")
            .on("keydown.enter", request)
        )
        with ui.row().classes("w-full justify-end"):
            ui.button("Send confirmation", on_click=request).props("dense")


def _password_card(
    *,
    email: str,
    stored_hash: str | None,
    must_retype: bool,
    ip: str,
    base_url: str,
) -> None:
    """Set or change the password, and drop it."""
    has_password = stored_hash is not None
    with ui.card().classes("w-full max-w-xl gap-3"):
        heading("Change your password" if has_password else "Set a password", level=2)
        current = None
        if must_retype:
            current = (
                required(ui.input("Current password", password=True))
                .props("outlined dense autocomplete=current-password")
                .classes("w-full")
                .mark("current-password")
            )
        elif has_password:
            ui.label(
                "You signed in with an emailed code, so you can set a new "
                "password without the old one."
            ).classes("text-sm text-gray-500")
        new_password = (
            required(
                ui.input("New password", password=True, password_toggle_button=True)
            )
            .props("outlined dense autocomplete=new-password")
            .classes("w-full")
            .mark("new-password")
        )
        ui.label(passwords.GUIDANCE).classes("text-xs text-gray-500")

        async def save() -> None:
            if not valid(current, new_password, repeat):
                return
            await _save_password(
                new_password.value or "",
                repeat.value or "",
                (current.value or "") if current is not None else "",
                email=email,
                stored_hash=stored_hash,
                must_retype=must_retype,
                ip=ip,
                base_url=base_url,
            )

        repeat = (
            required(
                ui.input(
                    "Repeat new password", password=True, password_toggle_button=True
                )
            )
            .props("outlined dense autocomplete=new-password")
            .classes("w-full")
            .mark("repeat-password")
            .on("keydown.enter", save)
        )
        with ui.row().classes("w-full justify-between items-center"):
            if has_password:
                ui.button("Remove password", on_click=_remove_password).props(
                    "flat dense color=negative"
                )
            else:
                ui.space()
            ui.button("Save password", on_click=save).props("dense")


@ui.page("/account")
async def account_page():
    async with page_ctx() as ctx:
        actor = ctx.actor
        user = actor.account
        pending = (
            user.pending_email
            if user_service.email_change_live(user, ctx.now)
            else None
        )
        pending_until = user.email_change_expires_at if pending else None
        # An emailed code (or a freshly redeemed invite) is possession of the
        # mailbox — the same proof a reset link carries, so it stands in for
        # the forgotten password.
        proved_by_email = session_auth_method() in ("otp", "invite")
        must_retype = user.password_hash is not None and not proved_by_email
        feed_token = expect(
            await user_service.ensure_calendar_token(
                ctx.session, user.id, token=ctx.env.rng.token()
            )
        )

    with frame("Your account", actor, help="account"):
        _signin_card(user.email, user.password_hash is not None)
        # a photo hangs off a volunteer record; an account without one (the
        # sync bot, an admin nobody linked) has nowhere to put a picture
        if actor.volunteer_id is not None:
            _photo_card(
                actor.volunteer_id, actor.volunteer_name or user.email, actor.photo_at
            )
        _calendar_card(ctx.base_url, feed_token)
        _email_card(
            user_id=user.id,
            base_url=ctx.base_url,
            pending=pending,
            pending_until=pending_until,
            tz=ctx.env.tz,
        )
        _password_card(
            email=user.email,
            stored_hash=user.password_hash,
            must_retype=must_retype,
            ip=ctx.ip,
            base_url=ctx.base_url,
        )
