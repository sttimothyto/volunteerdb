"""What a domain event implies, decided purely.

``plan`` is the subscriber side of the event/effect split: services emit
facts, this module says which mail goes where, which audit line is written
and which throttle is charged, and the edge interpreter (``effects.run``)
performs it. Everything a rule needs -- the time, the link base, the notify
mode, a snapshot of the throttle ledger, the parish copy -- arrives in
``PolicyCtx``; nothing here reads a clock, a setting or a database.

Phase 0 ships the shape only; the rules arrive with the events that raise
them (FUNCTIONAL_REFACTORING.md, Phase 4).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .domain import DomainEvent, NotifyMode
from .effects import Effect


@dataclass(frozen=True, slots=True)
class PolicyCtx:
    now: datetime
    base_url: str
    notify: NotifyMode
    # throttle.Ledger once the ledger is a value (Phase 1); mail.MailContext
    # once the templates take their copy as a parameter (Phase 3)
    throttle: Any = None
    copy: Any = None


def plan(events: Sequence[DomainEvent], ctx: PolicyCtx) -> tuple[Effect, ...]:
    return tuple(effect for event in events for effect in plan_one(event, ctx))


def plan_one(event: DomainEvent, ctx: PolicyCtx) -> tuple[Effect, ...]:
    return ()
