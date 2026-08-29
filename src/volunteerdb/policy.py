"""What a domain event implies, decided purely.

``plan`` is the subscriber side of the event/effect split: services emit
facts, this module says which mail goes where, which audit line is written
and which throttle is charged, and the edge interpreter (``effects.run``)
performs it. Everything a rule needs -- the time, the link base, the notify
mode, a snapshot of the throttle ledger, the parish copy -- arrives in
``PolicyCtx``; nothing here reads a clock, a setting or a database. The two
doors (GUI and JSON API) run the same rules over the same events, which is
what makes them agree: the API's Env carries ``NotifyMode.digest``, and
that one value is the whole of "the API sends no roster mail".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from . import throttle
from .domain import (
    AddressReplaced,
    ApiTokenIssued,
    CollaboratorAdded,
    DomainEvent,
    EmailChangeAttempted,
    EmailChangeCancelled,
    EmailChanged,
    EmailChangeRequested,
    EventCancelled,
    InviteIssued,
    InviteRedeemed,
    NotifyMode,
    OtpIssued,
    OtpRequested,
    PasswordChanged,
    SelfRemoved,
    SignedIn,
    SignInFailed,
    SlotHandedOver,
    SubClaimed,
    SubRequested,
)
from .effects import Audit, Effect, SendMail, ThrottleHit
from .services import mail


@dataclass(frozen=True, slots=True)
class PolicyCtx:
    now: datetime
    base_url: str
    notify: NotifyMode
    throttle: throttle.Ledger
    copy: mail.MailContext


def plan(events: Sequence[DomainEvent], ctx: PolicyCtx) -> tuple[Effect, ...]:
    return tuple(effect for event in events for effect in plan_one(event, ctx))


def plan_one(event: DomainEvent, ctx: PolicyCtx) -> tuple[Effect, ...]:
    """A door running ``digest`` sends no roster mail of its own (the JSON
    API): the people concerned hear from the nightly digest or not at all,
    exactly as before the two doors shared these rules. Audit lines are
    written either way."""
    match event:
        case SubRequested():
            return _sub_requested(event, ctx)
        case SlotHandedOver():
            return _slot_handed_over(event, ctx)
        case SubClaimed():
            return _sub_claimed(event, ctx)
        case SelfRemoved():
            return _self_removed(event, ctx)
        case EventCancelled():
            return _event_cancelled(event, ctx)
        case CollaboratorAdded(
            event_id=event_id, source_team_id=source, task_force_team_id=meta
        ):
            return (
                Audit(
                    "event.collaboration_added",
                    (
                        ("event_id", event_id),
                        ("source_team_id", source),
                        ("task_force_team_id", meta),
                    ),
                ),
            )
        # sign-in and the account: mailed and logged whichever door, because
        # these notices are the account's, not the roster's (SP 800-63B §4.1.2
        # wants them on a channel the session making the change cannot suppress)
        case OtpRequested(email=email, ip=ip):
            return (
                ThrottleHit(f"otp-ip:{ip}"),
                Audit("auth.otp_requested", (("email", email), ("ip", ip))),
            )
        case OtpIssued(email=email, code=code):
            return (SendMail(email, *mail.otp_email(code)),)
        case SignInFailed(email=email, ip=ip):
            # the per-account bucket AND the per-IP flood bucket (SP 800-63B
            # §3.2.2): a spray across many accounts from one place is throttled too
            return (ThrottleHit(f"pw:{email.lower()}"), ThrottleHit(f"pw-ip:{ip}"))
        case SignedIn(user_id=user_id, email=email, method=method, ip=ip):
            return (
                Audit(
                    "auth.login",
                    (("method", method), ("user", f"{user_id}:{email}"), ("ip", ip)),
                ),
            )
        case ApiTokenIssued(user_id=user_id, email=email, ip=ip):
            return (
                Audit(
                    "auth.api_token_issued",
                    (("user", f"{user_id}:{email}"), ("ip", ip)),
                ),
            )
        case PasswordChanged(user_id=user_id, email=email, removed=removed):
            return (
                Audit(
                    "auth.password_cleared" if removed else "auth.password_set",
                    (("user", f"{user_id}:{email}"),),
                ),
                SendMail(
                    email,
                    *mail.password_changed_email(
                        f"{ctx.base_url}/login", removed=removed
                    ),
                ),
            )
        case EmailChangeAttempted(user_id=user_id):
            return (ThrottleHit(f"email-change:{user_id}"),)
        case EmailChangeRequested():
            return _email_change_requested(event, ctx)
        case EmailChangeCancelled(user_id=user_id, email=email):
            return (
                Audit("auth.email_change_cancelled", (("user", f"{user_id}:{email}"),)),
            )
        case EmailChanged(user_id=user_id, was=was, now=now):
            # the receipt goes to the mailbox the account moved AWAY from
            # (§4.1.2): that address is owed the last word, and on a hijacked-
            # session takeover it is the only independent channel left
            return (
                Audit("auth.email_changed", (("user", f"{user_id}:{now}"),)),
                SendMail(
                    was, *mail.email_change_done_email(now, f"{ctx.base_url}/login")
                ),
            )
        case InviteIssued(user_id=user_id, email=email, token=token):
            # the link is a bearer credential: it goes to the address on the
            # volunteer's own record, and the audit line never carries it
            return (
                Audit(
                    "auth.invite_minted", (("account_id", user_id), ("address", email))
                ),
                SendMail(
                    email,
                    *mail.invite_email(f"{ctx.base_url}/invite/{token}", ctx=ctx.copy),
                ),
            )
        case AddressReplaced(volunteer_id=volunteer_id, was=was, now=now):
            audit = Audit(
                "volunteer.address_replaced_by_other",
                (
                    ("volunteer_id", volunteer_id),
                    ("was", was),
                    ("now", now or "(none)"),
                ),
            )
            if (
                not was or not now
            ):  # cleared rather than redirected: nowhere to point them
                return (audit,)
            return (
                audit,
                SendMail(was, *mail.address_edited_email(now, f"{ctx.base_url}/login")),
            )
        case InviteRedeemed(user_id=user_id, email=email, has_password=has_password):
            return (
                Audit(
                    "auth.invite_redeemed",
                    (("user", f"{user_id}:{email}"), ("confidentiality_agreed", True)),
                ),
                SendMail(
                    email,
                    *mail.welcome_email(
                        f"{ctx.base_url}/login", has_password=has_password
                    ),
                ),
            )
    return ()


def _email_change_requested(
    event: EmailChangeRequested, ctx: PolicyCtx
) -> tuple[Effect, ...]:
    """Both after the commit. The new address gets the proof -- it is only a
    claim until somebody reads mail there -- and the old address gets a
    warning it can still act on (§4.1.2: the notice travels a channel the
    browser making the change cannot suppress)."""
    return (
        Audit(
            "auth.email_change_requested",
            (("user", f"{event.user_id}:{event.old_email}"), ("to", event.new_email)),
        ),
        SendMail(
            event.new_email,
            *mail.email_change_email(
                f"{ctx.base_url}/confirm-email/{event.token}",
                event.new_email,
                event.ttl_hours,
                ctx=ctx.copy,
            ),
        ),
        SendMail(
            event.old_email,
            *mail.email_change_requested_email(
                event.new_email, f"{ctx.base_url}/account", event.ttl_hours
            ),
        ),
    )


def _when(starts_at: datetime, ends_at: datetime, ctx: PolicyCtx) -> str:
    return mail.event_when(starts_at, ends_at, tz=ctx.copy.tz)


def sub_request_key(team_id: int) -> str:
    return f"sub-req:{team_id}"


def _sub_requested(event: SubRequested, ctx: PolicyCtx) -> tuple[Effect, ...]:
    """The widest fan-out in the app -- one call mails every teammate not
    already serving -- so it is the one action rate-limited by volume rather
    than by abuse (throttle.LIMITS["sub-req"]). The request itself is never
    refused: it belongs on the events page whether or not it is announced. A
    team that has already sent its allowance today gets the row and no mail,
    and the edge tells the asker so (no SendMail among the effects)."""
    if ctx.notify is NotifyMode.digest:
        return ()
    key = sub_request_key(event.team_id)
    if throttle.blocked(ctx.throttle, key, ctx.now):
        return (
            Audit(
                "event.sub_request_capped",
                (("event_id", event.event_id), ("team_id", event.team_id)),
            ),
        )
    subject, body = mail.sub_request_email(
        event.title,
        event.path,
        event.slot,
        _when(event.starts_at, event.ends_at, ctx),
        event.asker,
        event.note,
        f"{ctx.base_url}/events",
    )
    return (
        ThrottleHit(key),
        *(SendMail(address, subject, body) for address in event.audience),
    )


def _slot_handed_over(event: SlotHandedOver, ctx: PolicyCtx) -> tuple[Effect, ...]:
    """Always logged -- who made it, and when, goes into the log. The incoming
    volunteer did not act, so they are told they are now scheduled: by mail
    right now when the edge runs ``direct`` (the GUI), by the nightly digest
    when it runs ``digest`` (the JSON API) -- the service stamped the notice
    row to match, which is why the mode is the event's, not the policy's."""
    effects: list[Effect] = [
        Audit(
            "event.slot_handed_over",
            (
                ("event_id", event.event_id),
                ("assignment_id", event.assignment_id),
                ("from_volunteer_id", event.outgoing_id),
                ("to_volunteer_id", event.incoming_id),
            ),
        )
    ]
    if event.notify is NotifyMode.direct and event.incoming_email:
        subject, body = mail.substituted_in_email(
            event.title,
            event.slot,
            _when(event.starts_at, event.ends_at, ctx),
            event.outgoing_name,
            f"{ctx.base_url}/events",
        )
        effects.append(SendMail(event.incoming_email, subject, body))
    return tuple(effects)


def _sub_claimed(event: SubClaimed, ctx: PolicyCtx) -> tuple[Effect, ...]:
    """The asker only. The team's leaders were copied here once and were the
    only recipients with nothing to do -- the message says a gap they never
    had to fill has been filled -- which on a three-leader team was four
    messages for one useful one. The event page carries the same fact, live,
    for whoever wants it."""
    if ctx.notify is NotifyMode.digest or not event.asker_email:
        return ()
    subject, body = mail.sub_claimed_email(
        event.title,
        event.slot,
        _when(event.starts_at, event.ends_at, ctx),
        event.claimant,
        event.asker,
    )
    return (SendMail(event.asker_email, subject, body),)


def _self_removed(event: SelfRemoved, ctx: PolicyCtx) -> tuple[Effect, ...]:
    """The reason goes to the team's leaders so they can fill the gap."""
    audit = Audit(
        "event.self_removal",
        (
            ("event_id", event.event_id),
            ("volunteer_id", event.volunteer_id),
            ("reason", event.reason[:500]),
        ),
    )
    if ctx.notify is NotifyMode.digest:
        return (audit,)
    subject, body = mail.self_removal_email(
        event.title,
        event.path,
        event.slot,
        _when(event.starts_at, event.ends_at, ctx),
        event.who,
        event.reason,
    )
    return (
        audit,
        *(SendMail(address, subject, body) for address in event.leader_emails),
    )


def _event_cancelled(event: EventCancelled, ctx: PolicyCtx) -> tuple[Effect, ...]:
    """Everyone signed up is told -- unless the event was already over, when
    nobody needs mail about it."""
    if ctx.notify is NotifyMode.digest or event.ends_at <= ctx.now:
        return ()
    subject, body = mail.event_cancelled_email(
        event.title, event.path, _when(event.starts_at, event.ends_at, ctx)
    )
    return tuple(SendMail(address, subject, body) for address in event.emails)
