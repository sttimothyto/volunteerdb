"""Accounts: the admin's list of who can sign in, and the ways to change it.

The page is an outline -- the two buttons, then one row per account -- and
every action is a module-level function taking the account's id, so each
can be read without the page around it.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from nicegui import ui

from .. import query_lang, timefmt
from ..domain import InviteIssued, Outcome
from ..effects import SendMail, delivered
from ..fp import Err, Ok, expect
from ..models import AppUser
from ..services import users as user_service
from ..services import volunteers as volunteer_service
from . import invites
from .context import PageCtx, flash, info, page_ctx, run_command
from .forms import actions, confirm, dialog_card, required, valid
from .guards import deny_unless_admin
from .layout import frame
from .tables import SearchedTable, count_text, wire_search
from .widgets import busy, empty_state

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
        email = (
            required(ui.input("Email (login)"))
            .props("outlined dense")
            .classes("w-full")
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
            if not valid(email):
                return

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

# The email cell carries the shield or person icon and the linked volunteer
# under the address; the status cell is the badge, clickable for a pending
# invite; the last cell is a "⋯" menu whose four actions are named in words
# instead of four icons whose meaning was in a tooltip. Each emits an event
# with its row (NiceGUI's table idiom); the handlers above take ids.
# the status badge: in its own column, and on a phone (where that column
# is hidden, theme.css .vdb-col-wide) under the address instead
_STATUS_BADGE = """
    <q-btn v-if="props.row.status === 'invite pending'" flat dense no-caps padding="none xs"
           :aria-label="'Invite pending for ' + props.row.email + ' — send it again'"
           @click.stop="$parent.$emit('pending', props.row)">
        <q-badge :color="props.row.status_color">
            {{ props.row.status }}
            <q-tooltip>{{ props.row.status_tooltip }}</q-tooltip>
        </q-badge>
    </q-btn>
    <q-badge v-else-if="props.row.status" :color="props.row.status_color">
        {{ props.row.status }}
        <q-tooltip>{{ props.row.status_tooltip }}</q-tooltip>
    </q-badge>
"""
# the address breaks after its @ on a phone (the <wbr>), never mid-word
_EMAIL_CELL = f"""
<q-td key="email" :props="props">
    <div class="flex no-wrap items-center gap-2">
        <q-icon :name="props.row.is_admin ? 'admin_panel_settings' : 'person'"
                :class="props.row.is_admin ? 'text-primary' : 'text-gray-400'" size="sm" />
        <div>
            <div class="font-medium">{{{{ props.row.email.split('@')[0] }}}}@<wbr>{{{{ props.row.email.split('@').slice(1).join('@') }}}}</div>
            <div class="text-xs text-gray-500">{{{{ props.row.linked }}}}</div>
            <div class="vdb-phone-only">{_STATUS_BADGE}</div>
        </div>
    </div>
</q-td>
"""
_STATUS_CELL = f"""
<q-td key="status" :props="props">{_STATUS_BADGE}</q-td>
"""
_ACTIONS_CELL = """
<q-td key="actions" :props="props">
    <q-btn-dropdown flat dense round no-icon-animation dropdown-icon="more_horiz"
                    :aria-label="'Actions for ' + props.row.email">
        <q-list dense>
            <q-item clickable v-close-popup @click.stop="$parent.$emit('relink', props.row)">
                <q-item-section avatar><q-icon name="link" /></q-item-section>
                <q-item-section>Change linked volunteer</q-item-section>
            </q-item>
            <q-item clickable v-close-popup @click.stop="$parent.$emit('admin', props.row)">
                <q-item-section avatar><q-icon :name="props.row.is_admin ? 'key_off' : 'key'" /></q-item-section>
                <q-item-section>{{ props.row.is_admin ? 'Revoke admin' : 'Make admin' }}</q-item-section>
            </q-item>
            <q-item clickable v-close-popup @click.stop="$parent.$emit('active', props.row)">
                <q-item-section avatar><q-icon :name="props.row.is_active ? 'block' : 'check_circle'" /></q-item-section>
                <q-item-section>{{ props.row.is_active ? 'Disable' : 'Enable' }}</q-item-section>
            </q-item>
            <q-item clickable v-close-popup @click.stop="$parent.$emit('reinvite', props.row)">
                <q-item-section avatar><q-icon name="mail" /></q-item-section>
                <q-item-section>New invite link (resets the password)</q-item-section>
            </q-item>
        </q-list>
    </q-btn-dropdown>
</q-td>
"""

ACCOUNT_COLUMNS = [
    {
        "name": "email",
        "label": "Account",
        "field": "email",
        "align": "left",
        "sortable": True,
    },
    {
        "name": "status",
        "label": "Status",
        "field": "status",
        "align": "left",
        "sortable": True,
        "classes": "vdb-col-wide",  # under the address on a phone
        "headerClasses": "vdb-col-wide",
    },
    {
        "name": "last_login",
        "label": "Last login",
        "field": "last_login_iso",  # ISO sorts; the cell shows the words
        "align": "left",
        "sortable": True,
        "classes": "vdb-col-wide",  # not on a phone
        "headerClasses": "vdb-col-wide",
    },
    {"name": "actions", "label": "", "field": "id", "align": "right"},
]


def _account_rows(
    accounts: list[AppUser],
    volunteer_names: dict[int, str],
    *,
    now: datetime,
    tz: ZoneInfo,
) -> list[dict]:
    """One row per account: who it is and who it is linked to, its sign-in
    state, and the day it last signed in. The precedence of the states
    matches the roster's (account_status.account_state)."""
    rows = []
    for account in accounts:
        if not account.is_active:
            status, color, tip = (
                "disabled",
                "muted",
                "Switched off: this account cannot sign in.",
            )
        elif user_service.invite_live(account, now=now):
            # No link on offer: only its digest is stored
            # (services.users._issue_invite), so handing one over again
            # means minting a fresh one -- which is what the re-invite does.
            until = account.invite_expires_at
            status, color, tip = (
                "invite pending",
                "warning",
                f"Invite link, usable until {timefmt.when_short(until, tz)}"
                if until
                else "Invite link outstanding",
            )
        elif account.invite_token:
            status, color, tip = (
                "invite expired",
                "muted",
                "The link has run out. They can still sign in with an emailed "
                "code; re-invite to hand out a fresh link.",
            )
        elif account.password_hash is None:
            status, color, tip = (
                "email-code sign-in",
                "info",
                "No password set — signs in with a one-time code emailed each time",
            )
        else:
            status, color, tip = "", "", ""
        rows.append(
            {
                "id": account.id,
                "email": account.email,
                "linked": (
                    volunteer_names.get(account.volunteer_id, "?")
                    if account.volunteer_id
                    else "not linked to a volunteer"
                ),
                "volunteer_id": account.volunteer_id,
                "is_admin": account.is_admin,
                "is_active": account.is_active,
                "status": status,
                "status_color": color,
                "status_tooltip": tip,
                "invite_until": (
                    account.invite_expires_at.isoformat()
                    if account.invite_expires_at
                    else ""
                ),
                "last_login": (
                    timefmt.day(account.last_login_at, tz)
                    if account.last_login_at
                    else ""
                ),
                "last_login_iso": (
                    account.last_login_at.isoformat() if account.last_login_at else ""
                ),
            }
        )
    return rows


def _matching_accounts(rows: list[dict], text: str) -> list[dict]:
    """The rows whose address, linked name or state contains `text`."""
    return [
        r
        for r in rows
        if any(text in (r[key] or "").lower() for key in ("email", "linked", "status"))
    ]


def _accounts_table(
    rows: list[dict],
    volunteer_names: dict[int, str],
    base_url: str,
    *,
    tz: ZoneInfo,
) -> None:
    """Every account as a table: searched, sorted, 25 to a page, one menu
    of named actions per row. Thirty-three accounts were 2,665 px of rows
    with four icon-only buttons each; four hundred would have been a page
    nobody scrolls."""
    with ui.row().classes("items-center gap-2 w-full"):
        search = (
            ui.input("Search accounts…")
            .props("outlined dense clearable debounce=200")
            .classes("grow")
            .mark("accounts-search")
        )
    table = (
        SearchedTable(
            columns=ACCOUNT_COLUMNS,
            rows=rows,
            row_key="id",
            pagination={"rowsPerPage": 25},
        )
        .props('rows-per-page-options="[25, 50, 100, 0]" hide-no-data')
        .classes("w-full")
        .mark("accounts")
    )
    table.add_slot("body-cell-email", _EMAIL_CELL)
    table.add_slot("body-cell-status", _STATUS_CELL)
    table.add_slot(
        "body-cell-last_login",
        '<q-td key="last_login" :props="props">{{ props.row.last_login }}</q-td>',
    )
    table.add_slot("body-cell-actions", _ACTIONS_CELL)
    table.on(
        "relink",
        lambda e: _relink_dialog(
            e.args["id"], e.args["email"], e.args["volunteer_id"], volunteer_names
        ),
    )
    table.on("admin", lambda e: _toggle_admin(e.args["id"], e.args["is_admin"]))
    table.on("active", lambda e: _toggle_active(e.args["id"], e.args["is_active"]))
    table.on("reinvite", lambda e: _reinvite(e.args["id"], e.args["email"], base_url))
    table.on(
        "pending",
        lambda e: invites.show_outstanding_invite(
            e.args["email"],
            datetime.fromisoformat(e.args["invite_until"])
            if e.args["invite_until"]
            else None,
            tz=tz,
        ),
    )
    count = ui.label(count_text(len(rows), None, "account")).classes(
        "text-sm text-gray-500"
    )
    wire_search(
        search,
        count,
        table,
        noun="account",
        compile=query_lang.compile_accounts,
        text_filter=_matching_accounts,
    )


@ui.page("/admin/users")
async def users_page():
    async with page_ctx() as ctx:
        session, actor, tz = ctx.session, ctx.actor, ctx.env.tz
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
    if deny_unless_admin(actor, "Accounts", help="accounts"):
        return

    with frame("Accounts", actor, help="accounts"):
        with ui.row().classes("gap-2"):
            # busy through the question and the work: the page button is
            # what the reader watches while forty invites go out
            ui.button(
                "Create accounts for all volunteers with email",
                icon="group_add",
                on_click=busy(_provision),
            ).props("dense")
            ui.button(
                "New account",
                icon="person_add",
                on_click=lambda: _new_account_dialog(volunteer_names, ctx.base_url),
            ).props("dense outline")
        if accounts:
            _accounts_table(
                _account_rows(accounts, volunteer_names, now=ctx.now, tz=tz),
                volunteer_names,
                ctx.base_url,
                tz=tz,
            )
        else:  # unreachable while the reader's own account is listed; kept honest
            empty_state(
                "No accounts yet.",
                action="New account",
                on_click=lambda: _new_account_dialog(volunteer_names, ctx.base_url),
            )
