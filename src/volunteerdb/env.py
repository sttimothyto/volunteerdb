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
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from . import db
from .config import Settings, settings
from .domain import NotifyMode


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


@dataclass(frozen=True, slots=True)
class Env:
    settings: Settings
    clock: Clock
    rng: Rng
    mailer: Mailer
    http: HttpClients
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    notify: NotifyMode = NotifyMode.direct

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings.timezone)

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
        notify=notify,
    )


def current() -> Env:
    """The Env the running app holds. Set by ``main.create_app``."""
    from nicegui import app

    env = getattr(app.state, "env", None)
    if env is None:
        raise RuntimeError("no Env: create_app() has not run in this process")
    return env
