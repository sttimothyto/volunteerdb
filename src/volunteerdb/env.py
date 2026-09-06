"""Env: everything impure the process needs, built once at a composition root.

The clock, the random source, the mailer, the HTTP client factory, the
database engine and its session factory, the settings. Services never see an
``Env``; they receive the VALUES they need (``now``, ``tz``, a token) from an
edge that holds one. There are exactly these composition roots: ``main.run``
(which hands it to ``create_app``), every ``jobs/*.cli``, ``admin_bootstrap``,
the seed, bench and share_roster_sheets scripts, and the test suite's ``env``
fixture and simulation main -- where the clock is a ``FakeClock``, the mailer
a recorder, and nothing reaches the network.

``current()`` reads the one the app holds (``nicegui.app.state.env``): a
``@ui.page`` function has no dependency injection, so the app object is the
carrier, and it survives the test harness re-running ``create_app()`` per
simulation because each run sets it again.

The mail transports live in ``mailers.py`` and the two mutable cells in
``cells.py``; both are re-exported here, so ``env.Smtp2goMailer`` and
``env.ThrottleCell`` still name them.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Protocol
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from . import db, passwords
from .cells import QuotaCell, ThrottleCell
from .config import Settings, settings
from .domain import NotifyMode
from .mailers import LoggingMailer as LoggingMailer
from .mailers import Smtp2goMailer as Smtp2goMailer
from .mailers import default_mailer
from .services import google_api, mail

if TYPE_CHECKING:
    from .services import users

log = structlog.get_logger(__name__)


class Clock(Protocol):
    def now(self) -> datetime:  # tz-aware, UTC
        ...


class Rng(Protocol):
    def token(self) -> str: ...
    def otp_code(self) -> str: ...
    def uuid(self) -> UUID: ...


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


class HttpxClients:
    def client(
        self, *, timeout: float = 10.0, follow_redirects: bool = False
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, follow_redirects=follow_redirects)


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

    def invite(self) -> "users.Invite":
        """What arming a sign-in link needs: a fresh token, the moment, and
        the configured lifetime (VDB_INVITE_TTL_HOURS)."""
        from .services.users import Invite

        return Invite(
            token=self.rng.token(),
            now=self.clock.now(),
            ttl=timedelta(hours=self.settings.invite_ttl_hours),
        )

    def mail_context(self) -> "mail.MailContext":
        """What the parish copy needs: the organisation's name, the invite
        lifetime, the zone times are written in."""
        s = self.settings
        return mail.MailContext(
            org=s.org_name, invite_ttl_hours=s.invite_ttl_hours, tz=self.tz
        )

    def google(self) -> google_api.GoogleConfig:
        """The parish Google grant (VDB_SHEETS_*) as a value, for the roster
        sheets and the calendar; the services never read the settings."""
        s = self.settings
        return google_api.GoogleConfig(
            client_id=s.sheets_client_id,
            client_secret=s.sheets_client_secret,
            refresh_token=s.sheets_refresh_token,
            folder_id=s.sheets_folder_id,
        )

    @property
    def password_terms(self) -> frozenset[str]:
        """This instance's own names, which the password policy refuses."""
        s = self.settings
        return passwords.site_terms(s.org_name, s.mail_from, s.public_base_url)

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
        engine = db.make_engine(config.database_url)
    sessions = db.make_sessions(engine)
    clock = clock if clock is not None else SystemClock()
    http = http if http is not None else HttpxClients()
    quota = QuotaCell()
    if mailer is None:
        mailer = default_mailer(
            config, http, sessions=sessions, quota=quota, clock=clock
        )
    return Env(
        settings=config,
        clock=clock,
        rng=rng if rng is not None else SecretsRng(),
        mailer=mailer,
        http=http,
        engine=engine,
        sessions=sessions,
        throttle=ThrottleCell(),
        quota=quota,
        notify=notify,
    )


def current() -> Env:
    """The Env the running app holds. Set by ``main.create_app``."""
    from nicegui import app

    env = getattr(app.state, "env", None)
    if env is None:
        raise RuntimeError("no Env: create_app() has not run in this process")
    return env
