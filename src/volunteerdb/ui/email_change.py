"""Changing the address you sign in with, from either page that offers it.

The /account page asks for the new address outright; the volunteer page's
edit dialog reaches the same flow when the address being edited is the
reader's own. Both spend the same budget and arm the same confirmation
link, so both do it here.
"""

from collections.abc import Callable
from datetime import datetime

from ..domain import EmailChangeAttempted
from ..services import users as user_service
from .context import PageCtx, flash, perform, rate_limit, run_command, toast


async def charged(user_id: int, *, base_url: str, now: datetime) -> bool:
    """Spend one attempt from the account's budget as of `now`; False, with
    the refusal toasted, when it has none left.

    The budget is on *sends*, not failures: what is worth abusing here is
    the parish's sender, one address at a time. Every attempt is charged,
    before the service can reveal whether the address is taken: a failed
    probe must count too, or it is an unthrottled account-existence oracle."""
    if denied := rate_limit(
        f"email-change:{user_id}", now=now, what="change your email address"
    ):
        toast(denied.error)
        return False
    await perform([EmailChangeAttempted(user_id)], base_url=base_url, now=now)
    return True


async def stage(address: str, *, sent: Callable[[str], str], reload: bool) -> None:
    """Arm the confirmation link for the signed-in account; the policy mails
    the address being claimed and warns the one being replaced, after the
    commit. `sent` words the line for the page that comes back, given the
    address now pending; `reload` False leaves the reload to the caller."""

    async def command(ctx: PageCtx):
        return await user_service.start_email_change(
            ctx.session,
            ctx.actor.account.id,
            address,
            now=ctx.now,
            token=ctx.env.rng.token(),
        )

    def done(value, _effects, _report) -> None:
        account, _token = value
        flash(sent(account.pending_email), multi_line=True)

    await run_command(command, on_ok=done, reload=reload)
