"""The two mutable holders a process keeps, both on the ``Env``.

Everything else in the tree is a value or a row. These two are the
exceptions the architecture names: the throttle ledger, which must be
shared by every request in the process and forgiven by a restart, and the
mail-allowance gauge, memoised for a minute so the admin header does not
cost a query per page. The arithmetic lives elsewhere (``throttle.py``,
``services/mail_quota.py``); a cell only holds the current value.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from . import throttle
from .services import mail_quota

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


class ThrottleCell:
    """The one mutable holder of the throttle ledger (throttle.py is the
    arithmetic). One process, one event loop: an update between awaits is
    atomic, and a restart forgives."""

    SWEEP_EVERY = 512

    def __init__(self) -> None:
        self._ledger = throttle.Ledger()
        self._since_sweep = 0

    def snapshot(self) -> throttle.Ledger:
        return self._ledger

    def blocked(self, key: str, now: datetime) -> bool:
        return throttle.blocked(self._ledger, key, now)

    def hit(self, key: str, now: datetime) -> None:
        self._ledger = throttle.hit(self._ledger, key, now)
        self._since_sweep += 1
        if self._since_sweep >= self.SWEEP_EVERY:
            self._since_sweep = 0
            self._ledger = throttle.prune(self._ledger, now)

    def reset(self) -> None:
        self._ledger = throttle.Ledger()
        self._since_sweep = 0


class QuotaCell:
    """The mail-allowance gauge every admin page header reads, memoised for a
    minute (services/mail_quota.py is the arithmetic and the ledger rows).
    Never raises: a gauge that cannot be computed is simply not shown."""

    TTL = timedelta(seconds=mail_quota.CACHE_TTL_SECONDS)

    def __init__(self) -> None:
        self._memo: tuple[datetime, mail_quota.Projection] | None = None

    async def projection(
        self,
        sessions: async_sessionmaker[AsyncSession],
        today: date,
        now: datetime,
    ) -> mail_quota.Projection:
        if self._memo is not None and now - self._memo[0] < self.TTL:
            return self._memo[1]
        try:
            async with sessions() as session:
                counts = await mail_quota.read_counts(session, today)
            result = mail_quota.project(counts, today)
        except Exception:  # noqa: BLE001 — a gauge must not break the page it sits on
            log.warning("mail_quota.projection_failed", exc_info=True)
            return mail_quota.Projection(0, 0, 0, 0, "", "")
        self._memo = (now, result)
        return result

    async def record(
        self, sessions: async_sessionmaker[AsyncSession], day: date
    ) -> None:
        """Count one message that actually left. Never raises: the counter
        exists to protect the mail, not the other way round."""
        try:
            async with sessions.begin() as session:
                await mail_quota.record(session, day)
        except Exception:  # noqa: BLE001 — see the docstring
            log.warning("mail_quota.record_failed", day=str(day), exc_info=True)

    def reset(self) -> None:
        self._memo = None
