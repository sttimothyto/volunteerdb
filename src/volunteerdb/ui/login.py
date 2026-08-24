import structlog
from fastapi import Request
from nicegui import ui
from starlette.responses import RedirectResponse

from .. import passwords, throttle
from ..config import settings
from ..db import db_session
from ..log import audit_log
from ..services import mail
from ..services import users as user_service
from .context import establish_session, session_user_id
from .logo_dialog import logo_img
from .theme import apply_theme

logger = structlog.get_logger(__name__)


def _safe_target(redirect_to: str) -> str:
    """Where a `redirect_to` query parameter is allowed to send a browser: a
    path on this site and nothing else. "//host" and "/\\host" are
    scheme-relative URLs, not same-origin paths."""
    safe = redirect_to.startswith("/") and not redirect_to.startswith(("//", "/\\"))
    return redirect_to if safe else "/"


@ui.page("/login")
def login_page(request: Request, redirect_to: str = "/"):
    # Already signed in: the card has nothing to offer. The public ministries
    # header offers "Sign in" to every reader — those pages are cached once for
    # the whole crowd, so they cannot know who is reading — and this is where
    # that link lands, so send the reader on to where it meant to take them.
    # A NiceGUI page builder may return a Response instead of building a page;
    # doing it here rather than over the websocket costs no round trip.
    if session_user_id() is not None:
        return RedirectResponse(_safe_target(redirect_to))

    apply_theme()

    pending_email = ""
    ip = request.client.host if request.client else "unknown"

    def finish(user_id: int, method: str) -> None:
        establish_session(user_id, remember=remember.value, method=method)
        ui.navigate.to(_safe_target(redirect_to))

    async def submit() -> None:
        addr = (email.value or "").strip()
        if not addr:
            ui.notify("Enter your email address", color="warning")
            return
        if password.value:
            keys = (f"pw:{addr.lower()}", f"pw-ip:{ip}")
            if throttle.blocked(keys[0], 5, 900) or throttle.blocked(keys[1], 30, 900):
                logger.warning("auth.throttled", method="password", email=addr, ip=ip)
                ui.notify(
                    "Too many failed attempts — try again in a few minutes.",
                    color="negative",
                )
                return
            async with db_session() as session:
                user = await user_service.authenticate(session, addr, password.value)
            if user is None:
                for key in keys:
                    throttle.hit(key)
                logger.warning(
                    "auth.login_failed", method="password", email=addr, ip=ip
                )
                ui.notify("Invalid email or password", color="negative")
                return
            audit_log(
                "auth.login", method="password", user=f"{user.id}:{user.email}", ip=ip
            )
            finish(user.id, "password")
        else:
            await send_code()

    async def send_code() -> None:
        nonlocal pending_email
        addr = (email.value or "").strip()
        if throttle.blocked(f"otp-ip:{ip}", 10, 3600):
            logger.warning("auth.throttled", method="otp", email=addr, ip=ip)
            ui.notify(
                "Too many code requests from this device — try again later.",
                color="negative",
            )
            return
        throttle.hit(f"otp-ip:{ip}")
        audit_log("auth.otp_requested", email=addr, ip=ip)
        async with db_session() as session:
            result = await user_service.start_otp_login(session, addr)
        if result is not None:
            user, code = result
            if code is not None:  # None: throttled, a live code is already out
                await mail.send_email(user.email, *mail.otp_email(code))
        # Identical response whether or not the account exists (no enumeration).
        pending_email = addr
        code_hint.set_text(f"Enter the 6-digit code emailed to {addr}")
        code_input.value = ""
        show_step(code_step)
        ui.notify("If that address has an account, a sign-in code is on its way.")

    async def verify() -> None:
        async with db_session() as session:
            user = await user_service.verify_otp(
                session, pending_email, code_input.value or ""
            )
        if user is None:
            logger.warning(
                "auth.login_failed", method="otp", email=pending_email, ip=ip
            )
            ui.notify(
                "That code didn't work — it may be mistyped or expired. "
                "Resend to get a fresh one.",
                color="negative",
            )
            return
        audit_log("auth.login", method="otp", user=f"{user.id}:{user.email}", ip=ip)
        finish(user.id, "otp")

    def show_step(step: ui.column) -> None:
        credentials_step.set_visibility(step is credentials_step)
        code_step.set_visibility(step is code_step)

    with ui.column().classes("absolute-center items-center gap-4"):
        # the parish's mark above its name; /logo serves the placeholder until
        # an admin uploads one, and this page has no session to gate on
        logo_img("/logo", "h-16 w-auto object-contain")
        ui.label("Volunteer Database (VDB)").classes("text-2xl vdb-brand")
        with ui.card().classes("w-80 gap-3"):
            with ui.column().classes("w-full gap-3") as credentials_step:
                # autocomplete=: NIST SP 800-63B §3.1.1.2 — "Verifiers SHALL
                # allow the use of password managers and autofill
                # functionality". The names are the WHATWG tokens managers
                # look for; password_toggle_button is the same section's
                # "SHOULD offer an option to display the password".
                email = (
                    ui.input("Email")
                    .props("outlined dense autocomplete=username")
                    .classes("w-full")
                    .on("keydown.enter", submit)
                )
                password = (
                    ui.input(
                        "Password (optional)",
                        password=True,
                        password_toggle_button=True,
                    )
                    .props("outlined dense autocomplete=current-password")
                    .classes("w-full")
                    .on("keydown.enter", submit)
                )
                ui.label(
                    "Leave the password blank and we'll email you a one-time code."
                ).classes("text-xs text-gray-500")
                ui.button("Sign in", on_click=submit).classes("w-full")
            with ui.column().classes("w-full gap-3") as code_step:
                code_hint = ui.label().classes("text-sm")
                code_input = (
                    ui.input("6-digit code")
                    .props(
                        "outlined dense inputmode=numeric autofocus "
                        "autocomplete=one-time-code"
                    )
                    .classes("w-full")
                    .on("keydown.enter", verify)
                )
                ui.button("Sign in with code", on_click=verify).classes("w-full")
                with ui.row().classes("w-full justify-between"):
                    ui.button("Resend code", on_click=send_code).props("flat dense")
                    ui.button(
                        "Different email", on_click=lambda: show_step(credentials_step)
                    ).props("flat dense")
            remember = ui.checkbox("Keep me signed in").tooltip(
                "Checked: stay signed in for 90 days on this device. Unchecked: 1 day."
            )
        code_step.set_visibility(False)
        ui.button("Browse ministry home pages", icon="public").props(
            'outline no-caps href="/ministries/"'
        ).classes("w-80")
        ui.label(
            f"Invite link is sent from {settings().mail_from}. Open it to finish setup."
        ).classes("text-sm text-gray-500 max-w-80 text-center")


@ui.page("/invite/{token}")
def invite_page(token: str, request: Request):
    apply_theme()
    login_url = f"{str(request.base_url).rstrip('/')}/login"

    async def redeem() -> None:
        if not agree.value:
            ui.notify(
                "To finish setup, please agree to keep personal information "
                "confidential.",
                color="warning",
            )
            return
        pw = password.value or ""
        if pw or confirm.value:
            # The service checks the policy too (it is the choke point); doing
            # it here as well is what turns a 500-shaped surprise into the
            # specific sentence the person needs while the form is still open.
            weak = passwords.problem(pw)
            if weak:
                ui.notify(weak, color="negative", multi_line=True, timeout=8000)
                return
            if pw != confirm.value:
                ui.notify("The two passwords don't match", color="negative")
                return
        async with db_session() as session:
            user = await user_service.redeem_invite(
                session, token, pw or None, agreed_to_confidentiality=agree.value
            )
        if user is None:
            logger.warning("auth.invite_invalid")
            ui.notify(
                "This link has expired or has already been used. You can still "
                "sign in: enter your email on the sign-in page and leave the "
                "password blank, and we'll email you a code.",
                color="negative",
                multi_line=True,
                timeout=10000,
            )
            return
        audit_log(
            "auth.invite_redeemed",
            user=f"{user.id}:{user.email}",
            confidentiality_agreed=True,
        )
        await mail.send_email(
            user.email, *mail.welcome_email(login_url, has_password=bool(pw))
        )
        establish_session(user.id, remember=remember.value, method="invite")
        ui.notify(
            "Welcome! Your password is set."
            if pw
            else "Welcome! We'll email you a code each time you sign in.",
            color="positive",
        )
        ui.navigate.to("/")

    with ui.column().classes("absolute-center items-center gap-4"):
        ui.label("Finish your account setup").classes("text-2xl vdb-brand")
        with ui.card().classes("w-80 gap-3"):
            ui.label(
                "Choosing a password is optional. If you skip it, you'll sign in "
                "with a one-time code emailed to you each time."
            ).classes("text-sm text-gray-500")
            password = (
                ui.input(
                    "Password (optional)", password=True, password_toggle_button=True
                )
                .props("outlined dense autocomplete=new-password")
                .classes("w-full")
            )
            # "Verifiers SHALL offer guidance to the subscriber to help the
            # subscriber choose a strong password" (§3.1.1.2) — up front, not
            # only as a complaint after the fact.
            ui.label(passwords.GUIDANCE).classes("text-xs text-gray-500")
            confirm = (
                ui.input("Repeat password", password=True, password_toggle_button=True)
                .props("outlined dense autocomplete=new-password")
                .classes("w-full")
                .on("keydown.enter", redeem)
            )
            remember = ui.checkbox("Keep me signed in").tooltip(
                "Checked: stay signed in for 90 days on this device. Unchecked: 1 day."
            )
            ui.separator()
            ui.label(
                "This database holds volunteers' personal information. By "
                "creating an account you agree to use it only for parish "
                "ministry and not to disclose anyone's personal information "
                "without their consent."
            ).classes("text-xs text-gray-500")
            agree = ui.checkbox("I agree to keep personal information confidential")
            ui.button("Finish setup and sign in", on_click=redeem).classes("w-full")


def confirm_email_url(base_url: str, token: str) -> str:
    return f"{base_url}/confirm-email/{token}"


@ui.page("/confirm-email/{token}")
async def confirm_email_page(token: str, request: Request):
    """The other end of a requested address change.

    Signed out on purpose (``/confirm-email/`` is in UNRESTRICTED_PREFIXES):
    the link goes to an address the person may only read on their phone, and
    a day later, long after the session that asked for it has gone. What
    authenticates them is possession of the token, which is exactly the claim
    being tested — asking them to sign in first would prove the wrong thing
    and, on a passwordless account, would mail a code to the address they are
    still trying to replace.

    Opening the link does not spend it: a mail scanner or a link prefetcher
    would then burn a single-use token before the recipient ever saw it. The
    button does. And unlike /invite/ this page signs nobody in — the link
    grants one address swap, never a session.

    Pressing it sends the outgoing address its last message: the address the
    account was reachable at is owed the news that it no longer is.
    """
    apply_theme()
    login_url = f"{str(request.base_url).rstrip('/')}/login"
    async with db_session() as session:
        account = await user_service.pending_email_change(session, token)
        target = account.pending_email if account is not None else None

    async def apply() -> None:
        try:
            async with db_session() as session:
                # confirm_email_change hands back the outgoing address: this
                # mailbox is owed the receipt (§4.1.2) for a binding that just
                # changed, and the instance is mutated in place
                result = await user_service.confirm_email_change(session, token)
        except ValueError as exc:  # the address went to somebody else first
            _show_dead_link(body, login_url, str(exc))
            return
        if result is None:
            logger.warning("auth.email_change_invalid")
            _show_dead_link(body, login_url)
            return
        user, was = result
        audit_log("auth.email_changed", user=f"{user.id}:{user.email}")
        settled = user.email
        if was:
            await mail.send_email(
                was, *mail.email_change_done_email(settled, login_url)
            )
        body.clear()
        with body:
            ui.label("Address confirmed").classes("text-2xl vdb-brand")
            with ui.card().classes("w-80 gap-3"):
                ui.label(settled).classes("font-medium")
                ui.label(
                    "This is now the address you sign in with, and the one "
                    "your ministries reach you at."
                ).classes("text-sm text-gray-500")
                ui.button("Sign in").props(f'href="{login_url}"').classes("w-full")

    body = ui.column().classes("absolute-center items-center gap-4")
    if target is None:
        logger.warning("auth.email_change_invalid")
        _show_dead_link(body, login_url)
    else:
        with body:
            ui.label("Confirm your new address").classes("text-2xl vdb-brand")
            with ui.card().classes("w-80 gap-3"):
                ui.label(target).classes("font-medium")
                ui.label(
                    "Confirming makes this the address you sign in with, and "
                    "the one on every ministry roster you serve on."
                ).classes("text-sm text-gray-500")
                ui.button("Confirm this address", on_click=apply).classes("w-full")


def _show_dead_link(body: ui.column, login_url: str, reason: str = "") -> None:
    """One card for every way a link can fail — unknown, spent, expired, or
    beaten to the address. No hint about which: the same no-oracle rule
    redeem_invite follows."""
    body.clear()
    with body:
        ui.label("That link did not work").classes("text-2xl vdb-brand")
        with ui.card().classes("w-80 gap-3"):
            ui.label(
                reason
                or "This link has expired or has already been used. Ask for "
                "the change again from the Password & sign-in page and we'll "
                "send a fresh one."
            ).classes("text-sm text-gray-500")
            ui.button("Sign in").props(f'href="{login_url}"').classes("w-full")
