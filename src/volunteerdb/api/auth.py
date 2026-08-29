import structlog
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from ..auth import async_verify_password
from ..db import transaction
from ..domain import ApiTokenIssued, EmailChangeAttempted, SignInFailed
from ..errors import NotFound, message
from ..fp import Err
from ..services import users as service
from .deps import CtxDep, dispatch, env_of, perform, raise_http, throttled
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
    env = env_of(request)
    now = env.clock.now()
    base_url = str(request.base_url).rstrip("/")
    addr = data.email.strip().lower()
    if throttled(env, f"pw:{addr}", f"pw-ip:{ip}", now=now):
        logger.warning("auth.throttled", method="api", email=data.email, ip=ip)
        raise HTTPException(429, "too many failed attempts; try again in a few minutes")
    async with transaction(env, None) as session:
        signed = await service.authenticate(session, data.email, data.password, now=now)
        if isinstance(signed, Err):
            await session.rollback()
        else:
            user = signed.value
            token = raise_http(
                await service.issue_api_token(session, user.id, token=env.rng.token())
            )
    if isinstance(signed, Err):
        logger.warning("auth.login_failed", method="api", email=data.email, ip=ip)
        await perform(env, [SignInFailed("api", addr, ip)], base_url=base_url, now=now)
        raise HTTPException(401, "invalid credentials")
    await perform(
        env, [ApiTokenIssued(user.id, user.email, ip)], base_url=base_url, now=now
    )
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
    ip = request.client.host if request.client else "unknown"
    # both buckets, exactly as POST /auth/login charges them: the per-account
    # limit and the per-IP flood limit, so a spray of current-password guesses
    # across many accounts from one IP is throttled at the IP too.
    env, now = ctx.env, ctx.now
    if throttled(env, f"pw:{email.lower()}", f"pw-ip:{ip}", now=now):
        raise HTTPException(429, "too many failed attempts; try again in a few minutes")
    if user.password_hash is None or not await async_verify_password(
        user.password_hash, data.current_password
    ):
        logger.warning("auth.password_change_denied", email=email, via="api")
        await perform(
            env, [SignInFailed("api", email, ip)], base_url=ctx.base_url, now=now
        )
        raise HTTPException(403, "that is not your current password")
    dispatch(
        ctx,
        background,
        await service.set_password(
            ctx.session, user.id, data.new_password, site_terms=env.password_terms
        ),
    )


@router.delete("/password", status_code=204)
async def clear_own_password(
    ctx: CtxDep, request: Request, background: BackgroundTasks
) -> None:
    """Drop back to emailed-code sign-in.

    This also revokes the API token making the call — it was issued against the
    password being removed — so it is the last request that token serves."""
    dispatch(
        ctx, background, await service.clear_password(ctx.session, ctx.actor.user.id)
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
    env, now = ctx.env, ctx.now
    if throttled(env, f"email-change:{user.id}", now=now):
        raise HTTPException(429, "too many address changes requested; try again later")
    # charge the budget on every attempt, before the service can reveal whether
    # the address exists: a probe that fails ("another account already signs in
    # with that address") must count too, or the budget is only on successful
    # sends and this is an unthrottled account-existence oracle.
    await perform(env, [EmailChangeAttempted(user.id)], base_url=ctx.base_url, now=now)
    account, _token = dispatch(
        ctx,
        background,
        await service.start_email_change(
            ctx.session, user.id, data.new_email, now=now, token=env.rng.token()
        ),
    )
    return PendingEmailOut(
        pending_email=account.pending_email,
        email_change_expires_at=account.email_change_expires_at,
    )


@router.delete("/email-change", status_code=204)
async def cancel_email_change(ctx: CtxDep, background: BackgroundTasks) -> None:
    """Call off a pending address change, killing its link. Idempotent."""
    dispatch(
        ctx,
        background,
        await service.cancel_email_change(ctx.session, ctx.actor.user.id),
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
    env = env_of(request)
    now = env.clock.now()
    async with transaction(env, None) as session:
        result = await service.confirm_email_change(session, data.token, now=now)
        match result:
            case Err(NotFound()):
                raise HTTPException(404, "that link is not valid any more")
            case Err(err):
                raise HTTPException(422, message(err))
        user, _was = result.value.value
        out = UserOut.model_validate(user)
        out.has_password = user.password_hash is not None
        out.invite_token = out.invite_expires_at = None
    # after the commit: the receipt to the mailbox the account moved AWAY from
    # (§4.1.2) is the policy's, from the EmailChanged event
    await perform(
        env, result.value.events, base_url=str(request.base_url).rstrip("/"), now=now
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
    env = env_of(request)
    now = env.clock.now()
    async with transaction(env, None) as session:
        redeemed = await service.redeem_invite(
            session,
            data.token,
            data.password,
            agreed_to_confidentiality=data.agreed_to_confidentiality,
            now=now,
            site_terms=env.password_terms,
        )
        match redeemed:
            case Err(NotFound()):
                raise HTTPException(404, "that link is not valid any more")
            case Err(err):
                raise HTTPException(422, message(err))
        user = redeemed.value.value
        out = UserOut.model_validate(user)
        out.has_password = user.password_hash is not None
        out.invite_token = out.invite_expires_at = None
    # after the commit: the welcome, from the InviteRedeemed event
    await perform(
        env, redeemed.value.events, base_url=str(request.base_url).rstrip("/"), now=now
    )
    return out
