from fastapi import Request
from nicegui import ui

from ..permissions import require
from ..services import users as user_service
from ..services import volunteers as volunteer_service
from . import invites
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
        volunteer_names = await volunteer_service.name_map(
            session, include_inactive=True
        )

    async def email_invite(address: str, token: str) -> bool:
        return await invites.email_invite(base_url, address, token)

    def show_invite(token: str, email: str, sent: bool | None = None) -> None:
        invites.show_invite(base_url, token, email, sent)

    with frame("Accounts", actor):
        with ui.row().classes("gap-2"):

            @notify_errors
            async def provision() -> None:
                with ui.dialog() as confirm_dialog, ui.card().classes("w-96 gap-3"):
                    ui.label(
                        "Create accounts for every active volunteer with an email "
                        "address and send each of them an invite email? Existing "
                        "accounts that aren't linked to anyone are linked to the "
                        "volunteer at the same address."
                    )
                    with ui.row().classes("justify-end w-full gap-2"):
                        ui.button(
                            "Cancel", on_click=lambda: confirm_dialog.submit(False)
                        ).props("flat")
                        ui.button(
                            "Create and email invites",
                            on_click=lambda: confirm_dialog.submit(True),
                        )
                if not await confirm_dialog:
                    return
                async with action_session() as (session, actor):
                    require(actor.is_admin, "manage accounts")
                    report = await user_service.bulk_provision(session)
                    created = [(u.email, u.invite_token) for _, u in report.created]
                    relinked, skipped = len(report.linked), len(report.skipped)
                emailed = sum(
                    [await email_invite(addr, token) for addr, token in created]
                )
                failed = len(created) - emailed
                ui.notify(
                    f"Created {len(created)} accounts ({emailed} invites emailed"
                    + (f", {failed} failed" if failed else "")
                    + ")"
                    + (
                        f", linked {relinked} existing accounts to their volunteer"
                        if relinked
                        else ""
                    )
                    + f", skipped {skipped}",
                    color="positive",
                )
                ui.navigate.reload()

            ui.button(
                "Create accounts for all volunteers with email",
                icon="group_add",
                on_click=provision,
            ).props("dense")
            ui.button(
                "New account", icon="person_add", on_click=lambda: new_account_dialog()
            ).props("dense outline")

        def new_account_dialog() -> None:
            with ui.dialog() as dialog, ui.card().classes("w-96 gap-3"):
                ui.label("New account").classes("text-lg font-medium")
                email = (
                    ui.input("Email (login)").props("outlined dense").classes("w-full")
                )
                link = (
                    ui.select(
                        {0: "— match by email —"} | volunteer_names,
                        label="Linked volunteer",
                        value=0,
                        with_input=True,
                    )
                    .props("outlined dense")
                    .classes("w-full")
                    .tooltip(
                        "Left as-is, the account is linked to the volunteer with "
                        "the same email address, if exactly one has it."
                    )
                )
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
                        token, addr = user.invite_token, user.email
                        matched = user.volunteer_id if not link.value else None
                    dialog.close()
                    if matched is not None:
                        ui.notify(
                            f"Linked to {volunteer_names.get(matched, matched)} "
                            "by email address",
                            color="info",
                        )
                    sent = await email_invite(addr, token)
                    show_invite(token, addr, sent)

                with ui.row().classes("justify-end w-full gap-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")
                    ui.button("Create", on_click=save)
            dialog.open()

        ui.label(f"{len(accounts)} accounts").classes("text-sm text-gray-500")
        for account in accounts:
            with ui.row().classes(
                "w-full items-center gap-3 p-2 rounded hover:bg-gray-100"
            ):
                ui.icon(
                    "admin_panel_settings" if account.is_admin else "person"
                ).classes(
                    "text-xl "
                    + ("text-primary" if account.is_admin else "text-gray-400")
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
                elif user_service.invite_live(account):
                    ui.badge("invite pending", color="warning").classes(
                        "cursor-pointer"
                    ).on(
                        "click",
                        lambda _, t=account.invite_token, m=account.email: show_invite(
                            t, m
                        ),
                    ).tooltip(
                        f"Invite link, usable until {account.invite_expires_at:%Y-%m-%d %H:%M}"
                    )
                elif account.invite_token:
                    ui.badge("invite expired", color="grey").tooltip(
                        "The link has run out. They can still sign in with an "
                        "emailed code; re-invite to hand out a fresh link."
                    )
                elif account.password_hash is None:
                    ui.badge("email-code sign-in", color="info").tooltip(
                        "No password set — signs in with a one-time code emailed each time"
                    )
                if account.last_login_at:
                    ui.label(f"last login {account.last_login_at:%Y-%m-%d}").classes(
                        "text-xs text-gray-400"
                    )

                @notify_errors
                async def toggle_admin(
                    _, uid=account.id, current=account.is_admin
                ) -> None:
                    async with action_session() as (session, actor):
                        require(actor.is_admin, "manage accounts")
                        await user_service.set_flags(session, uid, is_admin=not current)
                    ui.navigate.reload()

                @notify_errors
                async def toggle_active(
                    _, uid=account.id, current=account.is_active
                ) -> None:
                    async with action_session() as (session, actor):
                        require(actor.is_admin, "manage accounts")
                        await user_service.set_flags(
                            session, uid, is_active=not current
                        )
                    ui.navigate.reload()

                def relink_dialog(
                    uid=account.id, addr=account.email, current=account.volunteer_id
                ) -> None:
                    with ui.dialog() as dialog, ui.card().classes("w-96 gap-3"):
                        ui.label(f"Linked volunteer for {addr}").classes("font-medium")
                        pick = (
                            ui.select(
                                {0: "— not linked —"} | volunteer_names,
                                value=current or 0,
                                with_input=True,
                            )
                            .props("outlined dense")
                            .classes("w-full")
                            .mark(f"relink-pick-{uid}")
                        )

                        @notify_errors
                        async def save_link() -> None:
                            async with action_session() as (session, actor):
                                require(actor.is_admin, "manage accounts")
                                await user_service.set_volunteer(
                                    session, uid, pick.value or None
                                )
                            dialog.close()
                            ui.notify(
                                f"{addr} → {volunteer_names.get(pick.value, 'nobody')}",
                                color="positive",
                            )
                            ui.navigate.reload()

                        with ui.row().classes("justify-end w-full gap-2"):
                            ui.button("Cancel", on_click=dialog.close).props("flat")
                            ui.button("Save", on_click=save_link)
                    dialog.open()

                @notify_errors
                async def reinvite(_, uid=account.id, addr=account.email) -> None:
                    async with action_session() as (session, actor):
                        require(actor.is_admin, "manage accounts")
                        token = await user_service.reissue_invite(session, uid)
                    sent = await email_invite(addr, token)
                    show_invite(token, addr, sent)

                ui.button(icon="link", on_click=relink_dialog).props("dense flat").mark(
                    f"relink-{account.id}"
                ).tooltip("Change linked volunteer")
                ui.button(
                    icon="key_off" if account.is_admin else "key", on_click=toggle_admin
                ).props("dense flat").tooltip(
                    "Revoke admin" if account.is_admin else "Make admin"
                )
                ui.button(
                    icon="block" if account.is_active else "check_circle",
                    on_click=toggle_active,
                ).props("dense flat").tooltip(
                    "Disable" if account.is_active else "Enable"
                )
                ui.button(icon="mail", on_click=reinvite).props("dense flat").tooltip(
                    "New invite link (resets password)"
                )
