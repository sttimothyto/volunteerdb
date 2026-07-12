from nicegui import app, ui

from ..db import db_session
from ..services import users as user_service
from .theme import apply_theme


@ui.page("/login")
def login_page(redirect_to: str = "/"):
    apply_theme()

    async def try_login() -> None:
        async with db_session() as session:
            user = await user_service.authenticate(session, email.value or "", password.value or "")
        if user is None:
            ui.notify("Invalid email or password", color="negative")
            return
        app.storage.user["user_id"] = user.id
        ui.navigate.to(redirect_to if redirect_to.startswith("/") else "/")

    with ui.column().classes("absolute-center items-center gap-4"):
        ui.label("Volunteer Database (VDB)").classes("text-2xl vdb-brand")
        with ui.card().classes("w-80 gap-3"):
            email = ui.input("Email").props("outlined dense").classes("w-full").on(
                "keydown.enter", try_login
            )
            password = (
                ui.input("Password", password=True, password_toggle_button=True)
                .props("outlined dense")
                .classes("w-full")
                .on("keydown.enter", try_login)
            )
            ui.button("Sign in", on_click=try_login).classes("w-full")
        ui.label("Received an invite link? Open it to set your password.").classes(
            "text-sm text-gray-500"
        )


@ui.page("/invite/{token}")
def invite_page(token: str):
    apply_theme()

    async def redeem() -> None:
        if not password.value or len(password.value) < 8:
            ui.notify("Password must be at least 8 characters", color="negative")
            return
        if password.value != confirm.value:
            ui.notify("Passwords do not match", color="negative")
            return
        async with db_session() as session:
            user = await user_service.redeem_invite(session, token, password.value)
        if user is None:
            ui.notify("This invite link is invalid or already used", color="negative")
            return
        app.storage.user["user_id"] = user.id
        ui.notify("Welcome! Your password is set.", color="positive")
        ui.navigate.to("/")

    with ui.column().classes("absolute-center items-center gap-4"):
        ui.label("Set your password").classes("text-2xl vdb-brand")
        with ui.card().classes("w-80 gap-3"):
            password = (
                ui.input("New password", password=True, password_toggle_button=True)
                .props("outlined dense")
                .classes("w-full")
            )
            confirm = (
                ui.input("Repeat password", password=True)
                .props("outlined dense")
                .classes("w-full")
                .on("keydown.enter", redeem)
            )
            ui.button("Set password and sign in", on_click=redeem).classes("w-full")
