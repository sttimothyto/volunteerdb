"""Domain events: the facts a mutating service established, as values.

A service returns ``Outcome(value, events)``; it does not mail, log an audit
line, or touch a throttle. ``policy.plan`` turns the events into effects and
an edge interpreter performs them after the transaction committed. An event
carries what a mail template or an audit line needs -- names, times, the
addresses to reach -- and never a URL: links are edge data the policy adds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class NotifyMode(StrEnum):
    """How a volunteer who did not act themselves learns they are scheduled.

    ``direct``: the caller's policy mails them right after commit, so the
    nightly digest's "you have been scheduled" notice is stamped as already
    sent. ``digest``: nothing is mailed now and the digest tells them. The
    decision is made INSIDE the transaction (it is a row in ``notification``),
    which is why it is a parameter the edge states rather than something a
    post-commit policy could decide. The GUI runs ``direct``; the JSON API
    runs ``digest`` (docs/reference/http-api.md).
    """

    direct = "direct"
    digest = "digest"


@dataclass(frozen=True, slots=True)
class Outcome[T]:
    value: T
    events: tuple[DomainEvent, ...] = ()


# --- events -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SubRequested:
    team_id: int
    event_id: int
    title: str
    path: str
    slot: str
    starts_at: datetime
    ends_at: datetime
    asker: str
    note: str | None
    audience: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SlotHandedOver:
    event_id: int
    assignment_id: int
    title: str
    slot: str
    starts_at: datetime
    ends_at: datetime
    outgoing_id: int
    outgoing_name: str
    incoming_id: int
    incoming_email: str | None
    notify: NotifyMode


@dataclass(frozen=True, slots=True)
class SubClaimed:
    event_id: int
    sub_request_id: int
    title: str
    slot: str
    starts_at: datetime
    ends_at: datetime
    claimant: str
    asker: str
    asker_email: str | None


@dataclass(frozen=True, slots=True)
class SelfRemoved:
    event_id: int
    title: str
    path: str
    slot: str
    starts_at: datetime
    ends_at: datetime
    who: str
    volunteer_id: int | None
    reason: str
    leader_emails: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EventCancelled:
    event_id: int
    title: str
    path: str
    starts_at: datetime
    ends_at: datetime
    emails: tuple[str, ...]


# --- sign-in and the account: the edge states the request's own facts (an
# address, a method, a source IP) and the service states what it changed


@dataclass(frozen=True, slots=True)
class InviteIssued:
    user_id: int
    email: str
    token: str
    ttl_hours: int


@dataclass(frozen=True, slots=True)
class InviteRedeemed:
    user_id: int
    email: str
    has_password: bool


@dataclass(frozen=True, slots=True)
class OtpRequested:
    """A code was asked for -- charged and logged whether or not the
    address has an account, so the answer never says which."""

    email: str
    ip: str


@dataclass(frozen=True, slots=True)
class OtpIssued:
    email: str
    code: str


@dataclass(frozen=True, slots=True)
class SignInFailed:
    method: str
    email: str
    ip: str


@dataclass(frozen=True, slots=True)
class SignedIn:
    user_id: int
    email: str
    method: str
    ip: str


@dataclass(frozen=True, slots=True)
class ApiTokenIssued:
    user_id: int
    email: str
    ip: str


@dataclass(frozen=True, slots=True)
class PasswordChanged:
    user_id: int
    email: str
    removed: bool


@dataclass(frozen=True, slots=True)
class EmailChangeAttempted:
    """Asked, before the answer: the budget is charged on every attempt so
    a refused address cannot be probed for free."""

    user_id: int


@dataclass(frozen=True, slots=True)
class EmailChangeRequested:
    user_id: int
    old_email: str
    new_email: str
    token: str
    ttl_hours: int


@dataclass(frozen=True, slots=True)
class EmailChangeCancelled:
    user_id: int
    email: str


@dataclass(frozen=True, slots=True)
class EmailChanged:
    user_id: int
    was: str
    now: str


@dataclass(frozen=True, slots=True)
class AddressReplaced:
    volunteer_id: int
    was: str | None
    now: str | None


@dataclass(frozen=True, slots=True)
class RosterImported:
    outcome: str
    counts: tuple[tuple[str, int], ...]


type DomainEvent = (
    SubRequested
    | SlotHandedOver
    | SubClaimed
    | SelfRemoved
    | EventCancelled
    | InviteIssued
    | InviteRedeemed
    | OtpRequested
    | OtpIssued
    | SignInFailed
    | SignedIn
    | ApiTokenIssued
    | PasswordChanged
    | EmailChangeAttempted
    | EmailChangeRequested
    | EmailChangeCancelled
    | EmailChanged
    | AddressReplaced
    | RosterImported
)
