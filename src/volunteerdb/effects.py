"""Effects: instructions for the world, as values, and the one interpreter.

A service never sends mail, writes an audit line or touches a throttle; it
returns domain events (``domain.py``), ``policy.plan`` turns those into the
effects below, and an edge calls ``run`` AFTER its transaction committed --
mail never rides a transaction. ``run`` never raises: a failed effect is
counted and logged, because a toast or a 200 for work that did commit must
not turn into an error for work that merely failed to be announced.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from .log import audit_log

if TYPE_CHECKING:
    from .env import Env

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SendMail:
    to: str
    subject: str
    body: str


@dataclass(frozen=True, slots=True)
class Audit:
    event: str
    fields: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class ThrottleHit:
    key: str


type Effect = SendMail | Audit | ThrottleHit


@dataclass(frozen=True, slots=True)
class EffectReport:
    mailed: int = 0
    failed: int = 0


async def run(effects: Sequence[Effect], env: Env) -> EffectReport:
    """Perform every effect, in order. Never raises."""
    mailed = failed = 0
    for effect in effects:
        try:
            match effect:
                case SendMail(to, subject, body):
                    if await env.mailer.send(to, subject, body):
                        mailed += 1
                    else:
                        failed += 1
                case Audit(event, fields):
                    audit_log(event, **dict(fields))
                case ThrottleHit(key):
                    env.throttle.hit(key, env.clock.now())
        except Exception:  # noqa: BLE001 — see the module docstring
            failed += 1
            log.exception("effects.failed", effect=type(effect).__name__)
    return EffectReport(mailed=mailed, failed=failed)
