"""The door: sign in, redeem an invite, confirm a new address.

No page here has a signed-in actor, so none uses page_ctx(): each reads the
request's facts and the Env once and runs its own unit of work. The work
behind each button -- the throttle check, the service call, the events
performed after it -- is a module-level function taking the typed values;
the nested handlers only read the widgets and move between the steps.
"""

import html

import structlog
from fastapi import Request
from nicegui import ui
from starlette.responses import RedirectResponse

from .. import passwords
from ..api.deps import RequestFacts
from ..db import transaction
from ..domain import OtpRequested, SignedIn, SignInFailed
from ..env import Env, current
from ..errors import Invalid
from ..fp import Err, Ok
from ..models import AppUser
from ..services import users as user_service
from .a11y import heading
from .context import (
    establish_session,
    fail,
    flash,
    info,
    perform,
    rate_limit,
    session_user_id,
    toast,
    warn,
)
from .forms import required, valid
from .help_links import SIGNING_IN
from .logo_dialog import logo_img
from .theme import apply_theme

logger = structlog.get_logger(__name__)


def help_menu() -> None:
    """The user guide, offered at the door under one icon.

    The pages behind it are public (main.PUBLIC_MANUAL_PREFIXES) for exactly
    this reason: the reader who most needs "sign in with an emailed code" is
    the one who cannot sign in, and help kept behind the sign-in reaches
    everybody except them.

    Each opens in a new tab, as the dashboard's guide links do -- a half-typed
    address, or a 6-digit code with minutes left on it, survives the reading.
    """
    # Built out rather than with a11y.icon_button, for the same reason the
    # header's settings gear is: .tooltip() anchors under the icon, which is
    # where this button's own menu opens, and the two would sit on each other.
    with (
        ui.button(icon="help_outline")
        .props('flat round color=primary aria-label="Help signing in"')
        .classes("absolute-top-right q-ma-sm")
    ):
        # to the left, clear of the menu dropping down over the corner
        ui.tooltip(SIGNING_IN.title).props('anchor="center left" self="center right"')
        with ui.menu(), ui.column().classes("p-3 gap-2 w-72"):
            ui.label(SIGNING_IN.title).classes("text-sm font-medium")
            for link in SIGNING_IN.links:
                label = html.escape(link.title, quote=True)
                ui.button(link.title).props(
                    f'flat dense no-caps align=left href="{link.href}" '
                    f'target="_blank" rel="noopener" '
                    f'aria-label="{label} (opens in a new tab)"'
                ).classes("w-full")


def _safe_target(redirect_to: str) -> str:
    """Where a `redirect_to` query parameter is allowed to send a browser: a
    path on this site and nothing else. "//host" and "/\\host" are
    scheme-relative URLs, not same-origin paths."""
    safe = redirect_to.startswith("/") and not redirect_to.startswith(("//", "/\\"))
    return redirect_to if safe else "/"


# --- the sign-in page's work -----------------------------------------------------


async def _password_sign_in(
    addr: str, password: str, *, facts: RequestFacts, env: Env
) -> int | None:
    """The account's id when the password is right. A throttled or failed
    attempt is charged, logged and toasted here, and answers None."""
    now = env.clock.now()
    if denied := rate_limit(
        f"pw:{addr.lower()}", f"pw-ip:{facts.ip}", now=now, what="sign in"
    ):
        logger.warning("auth.throttled", method="password", email=addr, ip=facts.ip)
        toast(denied.error)
        return None
    async with transaction(env, None) as session:
        signed = await user_service.authenticate(session, addr, password, now=now)
    if isinstance(signed, Err):
        logger.warning("auth.login_failed", method="password", email=addr, ip=facts.ip)
        await perform(
            [SignInFailed("password", addr, facts.ip)],
            base_url=facts.base_url,
            now=now,
        )
        fail("Invalid email or password")
        return None
    user = signed.value
    await perform(
        [SignedIn(user.id, user.email, "password", facts.ip)],
        base_url=facts.base_url,
        now=now,
    )
    return user.id


async def _request_code(addr: str, *, facts: RequestFacts, env: Env) -> bool:
    """Mail a one-time code; True when the code step should open. The
    request is charged and logged whether or not the account exists (no
    enumeration); a fresh code -- the service says when, a live one is not
    resent -- is what gets mailed."""
    now = env.clock.now()
    if denied := rate_limit(
        f"otp-ip:{facts.ip}", now=now, what="request a sign-in code"
    ):
        logger.warning("auth.throttled", method="otp", email=addr, ip=facts.ip)
        toast(denied.error)
        return False
    async with transaction(env, None) as session:
        result = await user_service.start_otp_login(
            session, addr, now=now, code=env.rng.otp_code()
        )
    events = [OtpRequested(addr, facts.ip)]
    if isinstance(result, Ok):
        events.extend(result.value.events)
    await perform(events, base_url=facts.base_url, now=now)
    # Identical response whether or not the account exists (no enumeration).
    info("If that address has an account, a sign-in code is on its way.")
    return True


async def _verify_code(
    addr: str, code: str, *, facts: RequestFacts, env: Env
) -> int | None:
    """The account's id when the code is right; None once the refusal has
    been logged and toasted."""
    now = env.clock.now()
    async with transaction(env, None) as session:
        verified = await user_service.verify_otp(session, addr, code, now=now)
    if isinstance(verified, Err):
        logger.warning("auth.login_failed", method="otp", email=addr, ip=facts.ip)
        fail(
            "That code didn't work — it may be mistyped or expired. "
            "Resend to get a fresh one."
        )
        return None
    user = verified.value
    await perform(
        [SignedIn(user.id, user.email, "otp", facts.ip)],
        base_url=facts.base_url,
        now=now,
    )
    return user.id


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

    facts = RequestFacts.from_request(request)
    env = current()

    def finish(user_id: int, method: str) -> None:
        establish_session(user_id, remember=remember.value, method=method)
        ui.navigate.to(_safe_target(redirect_to))

    async def submit() -> None:
        if not valid(email):
            return
        addr = (email.value or "").strip()
        if not password.value:
            await send_code()
            return
        user_id = await _password_sign_in(addr, password.value, facts=facts, env=env)
        if user_id is not None:
            finish(user_id, "password")

    async def send_code() -> None:
        addr = (email.value or "").strip()
        if await _request_code(addr, facts=facts, env=env):
            code_hint.set_text(f"Enter the 6-digit code emailed to {addr}")
            code_input.value = ""
            show_step(code_step)

    async def verify() -> None:
        if not valid(code_input):
            return
        # the address the code went to is the one still in the (hidden) box
        addr = (email.value or "").strip()
        user_id = await _verify_code(addr, code_input.value or "", facts=facts, env=env)
        if user_id is not None:
            finish(user_id, "otp")

    def show_step(step: ui.column) -> None:
        credentials_step.set_visibility(step is credentials_step)
        code_step.set_visibility(step is code_step)

    help_menu()
    with ui.column().classes("absolute-center items-center gap-4"):
        # the parish's mark above its name; /logo serves the placeholder until
        # an admin uploads one, and this page has no session to gate on
        logo_img("/logo", "h-16 w-auto object-contain")
        heading("Volunteer Database (VDB)").classes("text-2xl vdb-brand")
        with ui.card().classes("w-80 gap-3"):
            with ui.column().classes("w-full gap-3") as credentials_step:
                # autocomplete=: NIST SP 800-63B §3.1.1.2 — "Verifiers SHALL
                # allow the use of password managers and autofill
                # functionality". The names are the WHATWG tokens managers
                # look for; password_toggle_button is the same section's
                # "SHOULD offer an option to display the password".
                email = (
                    required(ui.input("Email"))
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
                # the same emailed code, for the reader who does not know the
                # blank-password trick: a link-styled button, since the
                # address in the box is what it sends to
                ui.button(
                    "Forgot your password?",
                    on_click=lambda: send_code() if valid(email) else None,
                ).props("flat dense no-caps").classes("self-center text-sm").mark(
                    "forgot-password"
                )
            with ui.column().classes("w-full gap-3") as code_step:
                code_hint = ui.label().classes("text-sm")
                code_input = (
                    required(ui.input("6-digit code"))
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
            f"Invite link is sent from {env.settings.mail_from}. Open it to finish setup."
        ).classes("text-sm text-gray-500 max-w-80 text-center")


# --- the invite page's work ------------------------------------------------------


async def _redeem_invite(
    token: str,
    password: str,
    again: str,
    *,
    agreed: bool,
    facts: RequestFacts,
    env: Env,
) -> AppUser | None:
    """The account behind a redeemed link, with its events performed; None
    once the refusal has been toasted. The password is optional: given, it
    has to pass the policy and be typed twice."""
    if not agreed:
        warn("To finish setup, please agree to keep personal information confidential.")
        return None
    if password or again:
        # The service checks the policy too (it is the choke point); doing
        # it here as well is what turns a 500-shaped surprise into the
        # specific sentence the person needs while the form is still open.
        weak = passwords.problem(password, site_terms=env.password_terms)
        if weak:
            fail(weak, multi_line=True)
            return None
        if password != again:
            fail("The two passwords don't match")
            return None
    now = env.clock.now()
    async with transaction(env, None) as session:
        redeemed = await user_service.redeem_invite(
            session,
            token,
            password or None,
            agreed_to_confidentiality=agreed,
            now=now,
            site_terms=env.password_terms,
        )
    if isinstance(redeemed, Err):
        logger.warning("auth.invite_invalid", reason=type(redeemed.error).__name__)
        fail(
            "This link has expired or has already been used. You can still "
            "sign in: enter your email on the sign-in page and leave the "
            "password blank, and we'll email you a code.",
            multi_line=True,
            timeout=10000,
        )
        return None
    await perform(redeemed.value.events, base_url=facts.base_url, now=now)
    return redeemed.value.value


@ui.page("/invite/{token}")
def invite_page(token: str, request: Request):
    apply_theme()
    facts = RequestFacts.from_request(request)
    env = current()

    async def redeem() -> None:
        pw = password.value or ""
        user = await _redeem_invite(
            token,
            pw,
            confirm.value or "",
            agreed=bool(agree.value),
            facts=facts,
            env=env,
        )
        if user is None:
            return
        establish_session(user.id, remember=remember.value, method="invite")
        # the dashboard says it: a toast here would go with this page
        flash(
            "Welcome! Your password is set."
            if pw
            else "Welcome! We'll email you a code each time you sign in."
        )
        ui.navigate.to("/")

    # the same help as the sign-in page: "Sign in for the first time" is the
    # page written for the reader who is on this one
    help_menu()
    with ui.column().classes("absolute-center items-center gap-4"):
        heading("Finish your account setup").classes("text-2xl vdb-brand")
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


# --- the confirmation page's work ------------------------------------------------


async def _apply_email_change(
    token: str, body: ui.column, login_url: str, *, facts: RequestFacts, env: Env
) -> None:
    """Spend the token: the address moves, the outgoing one is mailed its
    last message, and `body` is redrawn with the outcome."""
    now = env.clock.now()
    async with transaction(env, None) as session:
        # the EmailChanged event names the outgoing address: that mailbox
        # is owed the receipt (§4.1.2) for a binding that just changed
        result = await user_service.confirm_email_change(session, token, now=now)
    match result:
        case Err(Invalid(text, _)):  # the address went to somebody else first
            _show_dead_link(body, login_url, text)
            return
        case Err():
            logger.warning("auth.email_change_invalid")
            _show_dead_link(body, login_url)
            return
    user, _was = result.value.value
    await perform(result.value.events, base_url=facts.base_url, now=now)
    body.clear()
    with body:
        heading("Address confirmed").classes("text-2xl vdb-brand")
        with ui.card().classes("w-80 gap-3"):
            ui.label(user.email).classes("font-medium")
            ui.label(
                "This is now the address you sign in with, and the one "
                "your ministries reach you at."
            ).classes("text-sm text-gray-500")
            ui.button("Sign in").props(f'href="{login_url}"').classes("w-full")


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
    facts = RequestFacts.from_request(request)
    login_url = f"{facts.base_url}/login"
    env = current()
    async with transaction(env, None) as session:
        account = await user_service.pending_email_change(
            session, token, now=env.clock.now()
        )
        target = account.pending_email if account is not None else None

    body = ui.column().classes("absolute-center items-center gap-4")
    if target is None:
        logger.warning("auth.email_change_invalid")
        _show_dead_link(body, login_url)
        return
    with body:
        heading("Confirm your new address").classes("text-2xl vdb-brand")
        with ui.card().classes("w-80 gap-3"):
            ui.label(target).classes("font-medium")
            ui.label(
                "Confirming makes this the address you sign in with, and "
                "the one on every ministry roster you serve on."
            ).classes("text-sm text-gray-500")
            ui.button(
                "Confirm this address",
                on_click=lambda: _apply_email_change(
                    token, body, login_url, facts=facts, env=env
                ),
            ).classes("w-full")


def _show_dead_link(body: ui.column, login_url: str, reason: str = "") -> None:
    """One card for every way a link can fail — unknown, spent, expired, or
    beaten to the address. No hint about which: the same no-oracle rule
    redeem_invite follows."""
    body.clear()
    with body:
        heading("That link did not work").classes("text-2xl vdb-brand")
        with ui.card().classes("w-80 gap-3"):
            ui.label(
                reason
                or "This link has expired or has already been used. Ask for "
                "the change again from the Your account page and we'll "
                "send a fresh one."
            ).classes("text-sm text-gray-500")
            ui.button("Sign in").props(f'href="{login_url}"').classes("w-full")
