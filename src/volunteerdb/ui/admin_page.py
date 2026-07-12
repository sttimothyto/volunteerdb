from fastapi import Request
from nicegui import ui

from ..permissions import require
from ..services import users as user_service
from ..services import volunteers as volunteer_service
from .context import action_session, notify_errors, page_session
from .layout import frame


@ui.page("/admin/users")
async def users_page(request: Request):
    base_url = str(request.base_url).rstrip("/")
    async with page_session() as (session, actor):
        if not actor.is_admin:
            with frame("Accounts", actor):
                ui.label("Admins only.").classes("text-gray-500")
            return
        accounts = await user_service.list_all(session)
        volunteers = await volunteer_service.search(session, include_inactive=True)
        volunteer_names = {v.id: v.full_name for v in volunteers}

    def invite_url(token: str) -> str:
        return f"{base_url}/invite/{token}"

    def show_invite(token: str, email: str) -> None:
        with ui.dialog() as dialog, ui.card().classes("gap-2 w-[34rem]"):
            ui.label(f"Invite link for {email}").classes("font-medium")
            url = invite_url(token)
            ui.input(value=url).props("readonly outlined dense").classes("w-full")
            ui.label("Hand this link to the volunteer (email, print, or in person).").classes(
                "text-sm text-gray-500"
            )
            with ui.row().classes("justify-end w-full gap-2"):
                ui.button(
                    "Copy",
                    on_click=lambda: (ui.clipboard.write(url), ui.notify("Copied")),
                ).props("dense")
                ui.button("Close", on_click=dialog.close).props("flat dense")
        dialog.open()

    with frame("Accounts", actor):
        with ui.row().classes("gap-2"):

            @notify_errors
            async def provision() -> None:
                async with action_session() as (session, actor):
                    require(actor.is_admin, "manage accounts")
                    report = await user_service.bulk_provision(session)
                ui.notify(
                    f"Created {len(report.created)} accounts, skipped {len(report.skipped)}",
                    color="positive",
                )
                ui.navigate.reload()

            ui.button(
                "Create accounts for all volunteers with email",
                icon="group_add",
                on_click=provision,
            ).props("dense")
            ui.button("New account", icon="person_add", on_click=lambda: new_account_dialog()).props(
                "dense outline"
            )

        def new_account_dialog() -> None:
            with ui.dialog() as dialog, ui.card().classes("w-96 gap-3"):
                ui.label("New account").classes("text-lg font-medium")
                email = ui.input("Email (login)").props("outlined dense").classes("w-full")
                link = ui.select(
                    {0: "— not linked —"} | volunteer_names,
                    label="Linked volunteer",
                    value=0,
                    with_input=True,
                ).props("outlined dense").classes("w-full")
                admin_flag = ui.switch("Parish admin (full access)")

                @notify_errors
                async def save() -> None:
                    async with action_session() as (session, actor):
                        require(actor.is_admin, "manage accounts")
                        user = await user_service.create(
                            session,
                            email.value or "",
                            volunteer_id=link.value or None,
                            is_admin=admin_flag.value,
                        )
                        token = user.invite_token
                    dialog.close()
                    show_invite(token, email.value)

                with ui.row().classes("justify-end w-full gap-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")
                    ui.button("Create", on_click=save)
            dialog.open()

        ui.label(f"{len(accounts)} accounts").classes("text-sm text-gray-500")
        for account in accounts:
            with ui.row().classes("w-full items-center gap-3 p-2 rounded hover:bg-gray-100"):
                ui.icon("admin_panel_settings" if account.is_admin else "person").classes(
                    "text-xl " + ("text-primary" if account.is_admin else "text-gray-400")
                )
                with ui.column().classes("gap-0"):
                    ui.label(account.email).classes("font-medium")
                    linked = (
                        volunteer_names.get(account.volunteer_id, "?")
                        if account.volunteer_id
                        else "not linked to a volunteer"
                    )
                    ui.label(linked).classes("text-xs text-gray-500")
                ui.space()
                if not account.is_active:
                    ui.badge("disabled", color="grey")
                elif account.password_hash is None and account.invite_token:
                    ui.badge("invite pending", color="warning").classes("cursor-pointer").on(
                        "click",
                        lambda _, t=account.invite_token, m=account.email: show_invite(t, m),
                    ).tooltip("Show invite link")
                if account.last_login_at:
                    ui.label(f"last login {account.last_login_at:%Y-%m-%d}").classes(
                        "text-xs text-gray-400"
                    )

                @notify_errors
                async def toggle_admin(_, uid=account.id, current=account.is_admin) -> None:
                    async with action_session() as (session, actor):
                        require(actor.is_admin, "manage accounts")
                        await user_service.set_flags(session, uid, is_admin=not current)
                    ui.navigate.reload()

                @notify_errors
                async def toggle_active(_, uid=account.id, current=account.is_active) -> None:
                    async with action_session() as (session, actor):
                        require(actor.is_admin, "manage accounts")
                        await user_service.set_flags(session, uid, is_active=not current)
                    ui.navigate.reload()

                @notify_errors
                async def reinvite(_, uid=account.id, mail=account.email) -> None:
                    async with action_session() as (session, actor):
                        require(actor.is_admin, "manage accounts")
                        token = await user_service.reissue_invite(session, uid)
                    show_invite(token, mail)

                ui.button(icon="key_off" if account.is_admin else "key", on_click=toggle_admin).props(
                    "dense flat"
                ).tooltip("Revoke admin" if account.is_admin else "Make admin")
                ui.button(
                    icon="block" if account.is_active else "check_circle", on_click=toggle_active
                ).props("dense flat").tooltip("Disable" if account.is_active else "Enable")
                ui.button(icon="mail", on_click=reinvite).props("dense flat").tooltip(
                    "New invite link (resets password)"
                )
