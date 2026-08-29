"""Env: everything impure the process needs, built once at a composition root.

The clock, the random source, the mailer, the HTTP client factory, the
database engine and its session factory, the settings. Services never see an
``Env``; they receive the VALUES they need (``now``, ``tz``, a token) from an
edge that holds one. There are exactly these composition roots: ``main.run``
(which hands it to ``create_app``), every ``jobs/*.cli``, ``admin_bootstrap``,
the seed and bench scripts, and the ``env`` test fixture -- where the clock is
a ``FakeClock``, the mailer a recorder, and nothing reaches the network.

``current()`` reads the one the app holds (``nicegui.app.state.env``): a
``@ui.page`` function has no dependency injection, so the app object is the
carrier, and it survives the test harness re-running ``create_app()`` per
simulation because each run sets it again.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from . import db, throttle
from .config import Settings, settings
from .domain import NotifyMode
from .services import mail_quota

log = structlog.get_logger(__name__)


class Clock(Protocol):
    def now(self) -> datetime:  # tz-aware, UTC
        ...


class Rng(Protocol):
    def token(self) -> str: ...
    def otp_code(self) -> str: ...
    def uuid(self) -> UUID: ...
    def hex(self, n: int) -> str: ...


class Mailer(Protocol):
    async def send(self, to: str, subject: str, body: str) -> bool: ...


class HttpClients(Protocol):
    def client(
        self, *, timeout: float = 10.0, follow_redirects: bool = False
    ) -> httpx.AsyncClient: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SecretsRng:
    def token(self) -> str:
        return secrets.token_urlsafe(32)

    def otp_code(self) -> str:
        return f"{secrets.randbelow(1_000_000):06d}"

    def uuid(self) -> UUID:
        return uuid4()

    def hex(self, n: int) -> str:
        return secrets.token_hex(n)


class HttpxClients:
    def client(
        self, *, timeout: float = 10.0, follow_redirects: bool = False
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, follow_redirects=follow_redirects)


class _SendEmailMailer:
    """Transition: the mail module's own transport, until it becomes a
    Mailer of its own (FUNCTIONAL_REFACTORING.md, Phase 3)."""

    async def send(self, to: str, subject: str, body: str) -> bool:
        from .services import mail

        return await mail.send_email(to, subject, body)


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


@dataclass(frozen=True, slots=True)
class Env:
    settings: Settings
    clock: Clock
    rng: Rng
    mailer: Mailer
    http: HttpClients
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    throttle: ThrottleCell
    quota: QuotaCell
    notify: NotifyMode = NotifyMode.direct

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings.timezone)

    def today(self) -> date:
        """The parish day: date-typed things mean 'end of that day HERE'."""
        return self.clock.now().astimezone(self.tz).date()

    def with_(self, **changes: object) -> Env:
        return replace(self, **changes)


def build(
    config: Settings | None = None,
    *,
    engine: AsyncEngine | None = None,
    clock: Clock | None = None,
    rng: Rng | None = None,
    mailer: Mailer | None = None,
    http: HttpClients | None = None,
    notify: NotifyMode = NotifyMode.direct,
) -> Env:
    """The real thing, with any part swappable. Composition roots only."""
    config = config if config is not None else settings()
    if engine is None:
        engine = db.engine()
        sessions = db.sessionmaker()
    else:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
    return Env(
        settings=config,
        clock=clock if clock is not None else SystemClock(),
        rng=rng if rng is not None else SecretsRng(),
        mailer=mailer if mailer is not None else _SendEmailMailer(),
        http=http if http is not None else HttpxClients(),
        engine=engine,
        sessions=sessions,
        throttle=ThrottleCell(),
        quota=QuotaCell(),
        notify=notify,
    )


def current() -> Env:
    """The Env the running app holds. Set by ``main.create_app``."""
    from nicegui import app

    env = getattr(app.state, "env", None)
    if env is None:
        raise RuntimeError("no Env: create_app() has not run in this process")
    return env
