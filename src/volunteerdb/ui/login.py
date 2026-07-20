from fastapi import Request
from nicegui import ui

from .. import throttle
from ..db import db_session
from ..services import mail
from ..services import users as user_service
from .context import establish_session
from .theme import apply_theme


@ui.page("/login")
def login_page(request: Request, redirect_to: str = "/"):
    apply_theme()

    pending_email = ""
    ip = request.client.host if request.client else "unknown"

    def finish(user_id: int) -> None:
        establish_session(user_id, remember=remember.value)
        # "//host" and "/\host" are scheme-relative URLs, not same-origin paths
        safe = redirect_to.startswith("/") and not redirect_to.startswith(("//", "/\\"))
        ui.navigate.to(redirect_to if safe else "/")

    async def submit() -> None:
        addr = (email.value or "").strip()
        if not addr:
            ui.notify("Enter your email address", color="warning")
            return
        if password.value:
            keys = (f"pw:{addr.lower()}", f"pw-ip:{ip}")
            if throttle.blocked(keys[0], 5, 900) or throttle.blocked(keys[1], 30, 900):
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
                ui.notify("Invalid email or password", color="negative")
                return
            finish(user.id)
        else:
            await send_code()

    async def send_code() -> None:
        nonlocal pending_email
        addr = (email.value or "").strip()
        if throttle.blocked(f"otp-ip:{ip}", 10, 3600):
            ui.notify(
                "Too many code requests from this device — try again later.",
                color="negative",
            )
            return
        throttle.hit(f"otp-ip:{ip}")
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
            user = await user_service.verify_otp(session, pending_email, code_input.value or "")
        if user is None:
            ui.notify(
                "That code didn't work — it may be mistyped or expired. "
                "Resend to get a fresh one.",
                color="negative",
            )
            return
        finish(user.id)

    def show_step(step: ui.column) -> None:
        credentials_step.set_visibility(step is credentials_step)
        code_step.set_visibility(step is code_step)

    with ui.column().classes("absolute-center items-center gap-4"):
        ui.label("Volunteer Database (VDB)").classes("text-2xl vdb-brand")
        with ui.card().classes("w-80 gap-3"):
            with ui.column().classes("w-full gap-3") as credentials_step:
                email = ui.input("Email").props("outlined dense").classes("w-full").on(
                    "keydown.enter", submit
                )
                password = (
                    ui.input("Password (optional)", password=True, password_toggle_button=True)
                    .props("outlined dense")
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
                    .props("outlined dense inputmode=numeric autofocus")
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
        ui.label(
            "No password? None needed — we'll email you a code. "
            "Received an invite link? Open it to finish setup."
        ).classes("text-sm text-gray-500 max-w-80 text-center")


@ui.page("/invite/{token}")
def invite_page(token: str, request: Request):
    apply_theme()
    login_url = f"{str(request.base_url).rstrip('/')}/login"

    async def redeem() -> None:
        pw = password.value or ""
        if pw or confirm.value:
            if len(pw) < 8:
                ui.notify("Password must be at least 8 characters", color="negative")
                return
            if pw != confirm.value:
                ui.notify("Passwords do not match", color="negative")
                return
        async with db_session() as session:
            user = await user_service.redeem_invite(session, token, pw or None)
        if user is None:
            ui.notify("This invite link is invalid or already used", color="negative")
            return
        await mail.send_email(user.email, *mail.welcome_email(login_url, has_password=bool(pw)))
        establish_session(user.id, remember=remember.value)
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
                ui.input("Password (optional)", password=True, password_toggle_button=True)
                .props("outlined dense")
                .classes("w-full")
            )
            confirm = (
                ui.input("Repeat password", password=True)
                .props("outlined dense")
                .classes("w-full")
                .on("keydown.enter", redeem)
            )
            remember = ui.checkbox("Keep me signed in").tooltip(
                "Checked: stay signed in for 90 days on this device. Unchecked: 1 day."
            )
            ui.button("Finish setup and sign in", on_click=redeem).classes("w-full")
