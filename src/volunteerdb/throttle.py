"""Sliding-window rate limits, as arithmetic over a value.

A ``Ledger`` is the hits each key has taken; ``blocked`` and ``hit`` are pure
functions over it, and ``hit`` returns a new ledger rather than changing one.
The single place a ledger is *kept* is ``env.ThrottleCell`` -- the app runs
one process on one event loop, so one cell is the whole state, and a restart
forgives, which at parish scale is the right trade for keeping this out of
the database.

Each key belongs to a family, named by the part before the first ``:``, and
the family fixes the limit: the numbers live here, once, with the reasons,
instead of at every call site that charges them.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class Limit:
    hits: int
    window: timedelta


LIMITS: Mapping[str, Limit] = {
    # failed password checks per account (SP 800-63B §3.2.2), and per source
    # address so a spray across many accounts from one place is throttled too
    "pw": Limit(5, timedelta(minutes=15)),
    "pw-ip": Limit(30, timedelta(minutes=15)),
    # sign-in codes requested per device: each one is a message spent
    "otp-ip": Limit(10, timedelta(hours=1)),
    # address-change mails per account: what is worth abusing is the parish's
    # sender, one address at a time -- charged on every attempt, before the
    # service can reveal whether the address is taken
    "email-change": Limit(5, timedelta(minutes=15)),
    # substitute calls a single team may broadcast in a rolling day. Every one
    # mails the whole roster minus the people already serving -- up to 28
    # messages a click on the largest team -- which makes this the one place a
    # handful of clicks can eat a day's mail allowance (services/mail_quota.py).
    # Six leaves an ordinary weekend's worth of genuine asks untouched.
    "sub-req": Limit(6, timedelta(days=1)),
}


@dataclass(frozen=True, slots=True)
class Ledger:
    """Hits per key, newest last. Never mutated: ``hit`` copies."""

    hits: Mapping[str, tuple[datetime, ...]] = field(default_factory=dict)


def limit_for(key: str) -> Limit:
    """The limit a key's family carries. An unknown family is a programming
    error, not a request to be lenient."""
    return LIMITS[key.partition(":")[0]]


def _live(hits: tuple[datetime, ...], cutoff: datetime) -> tuple[datetime, ...]:
    return tuple(t for t in hits if t > cutoff)


def blocked(ledger: Ledger, key: str, now: datetime) -> bool:
    """True once `key` has taken its family's limit inside the window."""
    limit = limit_for(key)
    return len(_live(ledger.hits.get(key, ()), now - limit.window)) >= limit.hits


def hit(ledger: Ledger, key: str, now: datetime) -> Ledger:
    """The ledger with one more hit on `key`; hits already outside the window
    are dropped on the way, so a key never grows past its window."""
    limit = limit_for(key)
    live = _live(ledger.hits.get(key, ()), now - limit.window) + (now,)
    return Ledger({**ledger.hits, key: live})


def prune(ledger: Ledger, now: datetime) -> Ledger:
    """Every key with a hit still inside its window, and nothing else. Keys are
    attacker-supplied (an address on the login throttle, an IP on the OTP one),
    so a stream of one-shot keys would otherwise leave an entry each, forever."""
    kept = {}
    for key, hits in ledger.hits.items():
        live = _live(hits, now - limit_for(key).window)
        if live:
            kept[key] = live
    return Ledger(kept)
