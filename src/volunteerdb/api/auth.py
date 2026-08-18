import structlog
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from .. import throttle
from ..auth import async_verify_password
from ..db import db_session
from ..log import audit_log
from ..services import mail
from ..services import users as service
from .deps import CtxDep
from .schemas import (
    EmailChangeConfirmIn,
    EmailChangeIn,
    LoginIn,
    PasswordIn,
    PendingEmailOut,
    RedeemInviteIn,
    TokenOut,
    UserOut,
)

router = APIRouter(prefix="/auth", tags=["auth"])
logger = structlog.get_logger(__name__)


@router.post("/login")
async def login(data: LoginIn, request: Request) -> TokenOut:
    """Exchange email+password for a fresh personal API token. Only a hash of
    the token is stored, so a new one is issued (revoking the old) on every
    login."""
    ip = request.client.host if request.client else "unknown"
    keys = (f"pw:{data.email.strip().lower()}", f"pw-ip:{ip}")
    if throttle.blocked(keys[0], 5, 900) or throttle.blocked(keys[1], 30, 900):
        logger.warning("auth.throttled", method="api", email=data.email, ip=ip)
        raise HTTPException(429, "too many failed attempts; try again in a few minutes")
    async with db_session() as session:
        user = await service.authenticate(session, data.email, data.password)
        if user is None:
            for key in keys:
                throttle.hit(key)
            logger.warning("auth.login_failed", method="api", email=data.email, ip=ip)
            raise HTTPException(401, "invalid credentials")
        token = await service.issue_api_token(session, user.id)
        audit_log("auth.api_token_issued", user=f"{user.id}:{user.email}", ip=ip)
    return TokenOut(token=token)


@router.get("/me")
async def me(ctx: CtxDep) -> UserOut:
    out = UserOut.model_validate(ctx.actor.user)
    out.has_password = ctx.actor.user.password_hash is not None
    out.invite_token = out.invite_expires_at = None
    return out


# --- the caller's own account -------------------------------------------------
#
# The GUI's /account page, over JSON. Two things it does NOT gain, on purpose:
#
# - **Emailed-code sign-in.** An API token is deliberately tied to a password
#   (issue_api_token, and clear_password revoking it), so an OTP-only account
#   cannot hold one. Minting a token from a mailbox round-trip would quietly
#   undo that, and an API client that wants one can set a password first.
# - **Changing somebody ELSE's address without confirmation.** That stays what
#   it is on PATCH /volunteers/{id}: immediate, and it mails the address it
#   moved away from.


@router.put("/password", status_code=204)
async def set_own_password(
    ctx: CtxDep, data: PasswordIn, request: Request, background: BackgroundTasks
) -> None:
    """Set or change the caller's own password.

    The current one is always required here (see PasswordIn), failed attempts
    charge the same throttle bucket as failed sign-ins for this account
    (SP 800-63B §3.2.2), and the new one is held to the policy in the service.
    A notice goes to the account's address afterwards — the change is worth
    hearing about on a channel the caller does not control."""
    user = ctx.actor.user
    email = user.email
    key = f"pw:{email.lower()}"
    if throttle.blocked(key, 5, 900):
        raise HTTPException(429, "too many failed attempts; try again in a few minutes")
    if user.password_hash is None or not await async_verify_password(
        user.password_hash, data.current_password
    ):
        throttle.hit(key)
        logger.warning("auth.password_change_denied", email=email, via="api")
        raise HTTPException(403, "that is not your current password")
    await service.set_password(ctx.session, user.id, data.new_password)
    audit_log("auth.password_set", user=f"{user.id}:{email}", via="api")
    base_url = str(request.base_url).rstrip("/")
    background.add_task(
        mail.send_email, email, *mail.password_changed_email(f"{base_url}/login")
    )


@router.delete("/password", status_code=204)
async def clear_own_password(
    ctx: CtxDep, request: Request, background: BackgroundTasks
) -> None:
    """Drop back to emailed-code sign-in.

    This also revokes the API token making the call — it was issued against the
    password being removed — so it is the last request that token serves."""
    user = ctx.actor.user
    email = user.email
    await service.clear_password(ctx.session, user.id)
    audit_log("auth.password_removed", user=f"{user.id}:{email}", via="api")
    base_url = str(request.base_url).rstrip("/")
    background.add_task(
        mail.send_email,
        email,
        *mail.password_changed_email(f"{base_url}/login", removed=True),
    )


@router.post("/email-change", status_code=202)
async def request_email_change(
    ctx: CtxDep, data: EmailChangeIn, request: Request, background: BackgroundTasks
) -> PendingEmailOut:
    """Ask to move this account to a new address.

    202, not 200: nothing has changed yet. A link goes to the address being
    claimed — it is only a claim until somebody reads mail there — and a warning
    goes to the address being replaced, while that one can still call it off.
    The throttle is on *sends*: what is worth abusing here is the parish's
    sender, one address at a time."""
    user = ctx.actor.user
    was = user.email
    key = f"email-change:{user.id}"
    if throttle.blocked(key, 5, 900):
        raise HTTPException(429, "too many address changes requested; try again later")
    account, token = await service.start_email_change(
        ctx.session, user.id, data.new_email
    )
    target = account.pending_email
    throttle.hit(key)
    audit_log("auth.email_change_requested", user=f"{user.id}:{was}", to=target)
    hours = int(service.EMAIL_CHANGE_TTL.total_seconds() // 3600)
    base_url = str(request.base_url).rstrip("/")
    background.add_task(
        mail.send_email,
        target,
        *mail.email_change_email(f"{base_url}/confirm-email/{token}", target, hours),
    )
    background.add_task(
        mail.send_email,
        was,
        *mail.email_change_requested_email(target, f"{base_url}/account", hours),
    )
    return PendingEmailOut(
        pending_email=target,
        email_change_expires_at=account.email_change_expires_at,
    )


@router.delete("/email-change", status_code=204)
async def cancel_email_change(ctx: CtxDep) -> None:
    """Call off a pending address change, killing its link. Idempotent."""
    await service.cancel_email_change(ctx.session, ctx.actor.user.id)
    audit_log(
        "auth.email_change_cancelled",
        user=f"{ctx.actor.user.id}:{ctx.actor.user.email}",
        via="api",
    )


@router.post("/email-change/confirm")
async def confirm_email_change(
    data: EmailChangeConfirmIn, request: Request, background: BackgroundTasks
) -> UserOut:
    """Spend the link mailed to the new address: the address moves, on the
    account and on the volunteer record behind it.

    Unauthenticated, like POST /auth/login — the token *is* the proof, and the
    person opening it may be doing so from a mailbox that has no session. An
    unknown, expired or already-spent link answers 404 the same way, saying
    nothing about which it was."""
    async with db_session() as session:
        user = await service.confirm_email_change(session, data.token)
        if user is None:
            raise HTTPException(404, "that link is not valid any more")
        out = UserOut.model_validate(user)
        out.has_password = user.password_hash is not None
        out.invite_token = out.invite_expires_at = None
        was_id, now = user.id, user.email
    audit_log("auth.email_change_confirmed", user=f"{was_id}:{now}", via="api")
    base_url = str(request.base_url).rstrip("/")
    background.add_task(
        mail.send_email, now, *mail.email_change_done_email(now, f"{base_url}/login")
    )
    return out


@router.post("/redeem-invite")
async def redeem_invite(
    data: RedeemInviteIn, request: Request, background: BackgroundTasks
) -> UserOut:
    """Spend an invite link and finish setting up the account.

    Unauthenticated for the same reason as the confirmation above: the token is
    the credential, and the account has no other one yet. The confidentiality
    agreement is refused in the service, not merely collected — nobody completes
    signup without it. With no password the account stays email-code-only, so a
    caller who wants API access should send one.

    The API mints invite tokens (POST /volunteers/{id}/invite, POST /users) but
    until now nothing could spend one."""
    async with db_session() as session:
        user = await service.redeem_invite(
            session,
            data.token,
            data.password,
            agreed_to_confidentiality=data.agreed_to_confidentiality,
        )
        if user is None:
            raise HTTPException(404, "that link is not valid any more")
        out = UserOut.model_validate(user)
        out.has_password = user.password_hash is not None
        out.invite_token = out.invite_expires_at = None
        addr, has_password = user.email, user.password_hash is not None
        audit_log(
            "auth.invite_redeemed",
            user=f"{user.id}:{addr}",
            confidentiality_agreed=True,
            via="api",
        )
    base_url = str(request.base_url).rstrip("/")
    background.add_task(
        mail.send_email,
        addr,
        *mail.welcome_email(f"{base_url}/login", has_password=has_password),
    )
    return out
