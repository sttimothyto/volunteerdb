from fastapi import Request
from nicegui import ui

from ..domain import InviteIssued, Outcome
from ..effects import SendMail, delivered
from ..env import current as current_env
from ..fp import Err, Ok
from ..services import users as user_service
from ..services import volunteers as volunteer_service
from . import invites
from .a11y import icon_button
from .context import PageCtx, page_session, run_command
from .layout import frame


@ui.page("/admin/users")
async def users_page(request: Request):
    base_url = str(request.base_url).rstrip("/")
    async with page_session() as (session, actor):
        if not actor.is_admin:
            with frame("Accounts", actor):
                ui.label("Admins only.").classes("text-gray-500")
            return
        accounts = (await user_service.list_all(session, actor)).unwrap()
        volunteer_names = await volunteer_service.name_map(
            session, include_inactive=True
        )

    def show_invite(token: str, email: str, sent: bool | None = None) -> None:
        invites.show_invite(base_url, token, email, sent)

    with frame("Accounts", actor):
        with ui.row().classes("gap-2"):

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

                async def command(ctx: PageCtx):
                    return await user_service.bulk_provision(
                        ctx.session, ctx.actor, mint=ctx.env.invite
                    )

                def done(report, effects, run) -> None:
                    # one invite mailed per account created, after the commit
                    created = len(report.created)
                    failed = sum(isinstance(e, SendMail) for e in effects) - run.mailed
                    relinked, skipped = len(report.linked), len(report.skipped)
                    ui.notify(
                        f"Created {created} accounts ({run.mailed} invites emailed"
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

                await run_command(command, on_ok=done)

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

                async def save() -> None:
                    async def command(ctx: PageCtx):
                        made = await user_service.create(
                            ctx.session,
                            email.value or "",
                            actor=ctx.actor,
                            volunteer_id=link.value or None,
                            is_admin=admin_flag.value,
                            invite=ctx.env.invite(),
                        )
                        if isinstance(made, Err):
                            return made
                        user, token = made.value
                        # no password here, so create() armed a link: that is
                        # an invite minted, and the policy mails it
                        issued = (
                            (
                                InviteIssued(
                                    user.id,
                                    user.email,
                                    token,
                                    ctx.env.settings.invite_ttl_hours,
                                ),
                            )
                            if token
                            else ()
                        )
                        return Ok(Outcome((user, token), issued))

                    def done(value, effects, run) -> None:
                        user, token = value
                        dialog.close()
                        matched = user.volunteer_id if not link.value else None
                        if matched is not None:
                            ui.notify(
                                f"Linked to {volunteer_names.get(matched, matched)} "
                                "by email address",
                                color="info",
                            )
                        if token:
                            show_invite(token, user.email, delivered(effects, run))

                    await run_command(command, on_ok=done, reload=False)

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
                    ui.badge("disabled", color="muted")
                elif user_service.invite_live(account, now=current_env().clock.now()):
                    # No link on offer: only its digest is stored
                    # (services.users._issue_invite), so handing one over again
                    # means minting a fresh one — which is what Reinvite does.
                    ui.badge("invite pending", color="warning").classes(
                        "cursor-pointer"
                    ).on(
                        "click",
                        lambda _, m=account.email, u=account.invite_expires_at: (
                            invites.show_outstanding_invite(m, u)
                        ),
                    ).tooltip(
                        f"Invite link, usable until {account.invite_expires_at:%Y-%m-%d %H:%M}"
                    )
                elif account.invite_token:
                    ui.badge("invite expired", color="muted").tooltip(
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

                async def toggle_admin(
                    _, uid=account.id, current=account.is_admin
                ) -> None:

                    async def command(ctx: PageCtx):
                        return await user_service.set_flags(
                            ctx.session, uid, actor=ctx.actor, is_admin=not current
                        )

                    await run_command(command, reload=True)

                async def toggle_active(
                    _, uid=account.id, current=account.is_active
                ) -> None:

                    async def command(ctx: PageCtx):
                        return await user_service.set_flags(
                            ctx.session, uid, actor=ctx.actor, is_active=not current
                        )

                    await run_command(command, reload=True)

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

                        async def save_link() -> None:

                            async def command(ctx: PageCtx):
                                return await user_service.set_volunteer(
                                    ctx.session,
                                    uid,
                                    pick.value or None,
                                    actor=ctx.actor,
                                )

                            def done(_value, _effects, _report) -> None:
                                dialog.close()
                                ui.notify(
                                    f"{addr} → {volunteer_names.get(pick.value, 'nobody')}",
                                    color="positive",
                                )

                            await run_command(command, on_ok=done, reload=True)

                        with ui.row().classes("justify-end w-full gap-2"):
                            ui.button("Cancel", on_click=dialog.close).props("flat")
                            ui.button("Save", on_click=save_link)
                    dialog.open()

                async def reinvite(_, uid=account.id, addr=account.email) -> None:
                    async def command(ctx: PageCtx):
                        return await user_service.reissue_invite(
                            ctx.session, uid, actor=ctx.actor, invite=ctx.env.invite()
                        )

                    await run_command(
                        command,
                        on_ok=lambda token, effects, run: show_invite(
                            token, addr, delivered(effects, run)
                        ),
                        reload=False,
                    )

                icon_button(
                    "link", "Change linked volunteer", on_click=relink_dialog
                ).props("dense flat").mark(f"relink-{account.id}")
                icon_button(
                    "key_off" if account.is_admin else "key",
                    "Revoke admin" if account.is_admin else "Make admin",
                    on_click=toggle_admin,
                ).props("dense flat")
                icon_button(
                    "block" if account.is_active else "check_circle",
                    "Disable" if account.is_active else "Enable",
                    on_click=toggle_active,
                ).props("dense flat")
                icon_button(
                    "mail", "New invite link (resets password)", on_click=reinvite
                ).props("dense flat")
