"""Accounts: the admin's list of who can sign in, and the ways to change it.

The page is an outline -- the two buttons, then one row per account -- and
every action is a module-level function taking the account's id, so each
can be read without the page around it.
"""

from datetime import datetime

from nicegui import ui

from ..domain import InviteIssued, Outcome
from ..effects import SendMail, delivered
from ..fp import Err, Ok, expect
from ..models import AppUser
from ..services import users as user_service
from ..services import volunteers as volunteer_service
from . import invites
from .a11y import icon_button
from .context import PageCtx, flash, info, page_ctx, run_command
from .forms import actions, confirm, dialog_card
from .guards import deny_unless_admin
from .layout import frame

# --- actions -------------------------------------------------------------------


async def _provision() -> None:
    """Accounts for every active volunteer with an address, each mailed an
    invite after the commit."""
    if not await confirm(
        "Create accounts for every active volunteer with an email "
        "address and send each of them an invite email? Existing "
        "accounts that aren't linked to anyone are linked to the "
        "volunteer at the same address.",
        yes="Create and email invites",
    ):
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
        flash(
            f"Created {created} accounts ({run.mailed} invites emailed"
            + (f", {failed} failed" if failed else "")
            + ")"
            + (
                f", linked {relinked} existing accounts to their volunteer"
                if relinked
                else ""
            )
            + f", skipped {skipped}",
            multi_line=True,
        )

    await run_command(command, on_ok=done)


def _new_account_dialog(volunteer_names: dict[int, str], base_url: str) -> None:
    with dialog_card("New account") as dialog:
        email = ui.input("Email (login)").props("outlined dense").classes("w-full")
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
                    info(
                        f"Linked to {volunteer_names.get(matched, matched)} "
                        "by email address"
                    )
                if token:
                    invites.show_invite(
                        base_url, token, user.email, delivered(effects, run)
                    )

            await run_command(command, on_ok=done, reload=False)

        actions(dialog, "Create", save)
    dialog.open()


async def _toggle_admin(user_id: int, is_admin: bool) -> None:
    async def command(ctx: PageCtx):
        return await user_service.set_flags(
            ctx.session, user_id, actor=ctx.actor, is_admin=not is_admin
        )

    await run_command(
        command,
        reload=True,
        success="Admin right revoked" if is_admin else "Admin right granted",
    )


async def _toggle_active(user_id: int, is_active: bool) -> None:
    async def command(ctx: PageCtx):
        return await user_service.set_flags(
            ctx.session, user_id, actor=ctx.actor, is_active=not is_active
        )

    await run_command(
        command,
        reload=True,
        success="Account disabled" if is_active else "Account enabled",
    )


def _relink_dialog(
    user_id: int,
    email: str,
    current_volunteer_id: int | None,
    volunteer_names: dict[int, str],
) -> None:
    with dialog_card(f"Linked volunteer for {email}") as dialog:
        pick = (
            ui.select(
                {0: "— not linked —"} | volunteer_names,
                value=current_volunteer_id or 0,
                with_input=True,
            )
            .props("outlined dense")
            .classes("w-full")
            .mark(f"relink-pick-{user_id}")
        )

        async def save_link() -> None:
            async def command(ctx: PageCtx):
                return await user_service.set_volunteer(
                    ctx.session, user_id, pick.value or None, actor=ctx.actor
                )

            def done(_value, _effects, _report) -> None:
                dialog.close()
                flash(f"{email} → {volunteer_names.get(pick.value, 'nobody')}")

            await run_command(command, on_ok=done, reload=True)

        actions(dialog, "Save", save_link)
    dialog.open()


async def _reinvite(user_id: int, email: str, base_url: str) -> None:
    """A fresh link (which resets the password), shown and mailed -- after a
    question, because the icon sits beside three harmless ones and this one
    locks a person out until they open their mail."""
    if not await confirm(
        f"Send {email} a new invite link?",
        detail=(
            "Their password is removed and any link already mailed stops "
            "working. The new link is emailed to them and shown to you."
        ),
        yes="Send a new invite link",
        icon="mail",
        danger=True,
    ):
        return

    async def command(ctx: PageCtx):
        return await user_service.reissue_invite(
            ctx.session, user_id, actor=ctx.actor, invite=ctx.env.invite()
        )

    await run_command(
        command,
        on_ok=lambda token, effects, run: invites.show_invite(
            base_url, token, email, delivered(effects, run)
        ),
        reload=False,
    )


# --- the page ------------------------------------------------------------------


def _account_row(
    account: AppUser,
    volunteer_names: dict[int, str],
    base_url: str,
    *,
    now: datetime,
) -> None:
    """One account: who it is and who it is linked to, its sign-in state,
    and the four controls -- relink, admin, disable, reinvite."""
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
            ui.badge("disabled", color="muted")
        elif user_service.invite_live(account, now=now):
            # No link on offer: only its digest is stored
            # (services.users._issue_invite), so handing one over again
            # means minting a fresh one — which is what Reinvite does.
            ui.badge("invite pending", color="warning").classes("cursor-pointer").on(
                "click",
                lambda: invites.show_outstanding_invite(
                    account.email, account.invite_expires_at
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
        icon_button(
            "link",
            "Change linked volunteer",
            on_click=lambda: _relink_dialog(
                account.id, account.email, account.volunteer_id, volunteer_names
            ),
        ).props("dense flat").mark(f"relink-{account.id}")
        icon_button(
            "key_off" if account.is_admin else "key",
            "Revoke admin" if account.is_admin else "Make admin",
            on_click=lambda: _toggle_admin(account.id, account.is_admin),
        ).props("dense flat")
        icon_button(
            "block" if account.is_active else "check_circle",
            "Disable" if account.is_active else "Enable",
            on_click=lambda: _toggle_active(account.id, account.is_active),
        ).props("dense flat")
        icon_button(
            "mail",
            "New invite link (resets password)",
            on_click=lambda: _reinvite(account.id, account.email, base_url),
        ).props("dense flat").mark(f"reinvite-{account.id}")


@ui.page("/admin/users")
async def users_page():
    async with page_ctx() as ctx:
        session, actor = ctx.session, ctx.actor
        accounts = (
            expect(await user_service.list_all(session, actor))
            if actor.is_admin
            else []
        )
        volunteer_names = (
            await volunteer_service.name_map(session, include_inactive=True)
            if actor.is_admin
            else {}
        )
    if deny_unless_admin(actor, "Accounts"):
        return

    with frame("Accounts", actor):
        with ui.row().classes("gap-2"):
            ui.button(
                "Create accounts for all volunteers with email",
                icon="group_add",
                on_click=_provision,
            ).props("dense")
            ui.button(
                "New account",
                icon="person_add",
                on_click=lambda: _new_account_dialog(volunteer_names, ctx.base_url),
            ).props("dense outline")
        ui.label(f"{len(accounts)} accounts").classes("text-sm text-gray-500")
        for account in accounts:
            _account_row(account, volunteer_names, ctx.base_url, now=ctx.now)
