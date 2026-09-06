"""The two mail transports behind ``Env.mailer``.

``Smtp2goMailer`` is the real one: the SMTP2GO HTTPS API, never raising,
counting each message that actually left into the mail-allowance ledger.
``LoggingMailer`` stands in when no API key is configured, and is careful
about what it prints. ``default_mailer`` picks between them from the
settings. Everything else the app mails goes through ``services/mail.py``
(the templates) and ``effects.py`` (the interpreter); this module is only
the wire.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import httpx
import structlog

from .config import Settings
from .services import mail

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from .cells import QuotaCell
    from .env import Clock, HttpClients, Mailer

log = structlog.get_logger(__name__)


class LoggingMailer:
    """No API key: nothing is sent. The body goes to stdout only under
    VDB_DEBUG_MAIL or VDB_RELOAD (`make dev`), because these bodies carry
    sign-in codes and invite links -- a production instance that merely
    forgot the key must not write every credential it issues into journald,
    where the app's own log redaction cannot reach it."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send(self, to: str, subject: str, body: str) -> bool:
        s = self._settings
        if s.debug_mail or s.reload:
            print(f"[MAIL] to={to} subject={subject!r}\n{body}", flush=True)
        else:
            log.warning(
                "mail.not_configured",
                to=to,
                subject=subject,
                hint="set VDB_SMTP2GO_API_KEY to send, or VDB_DEBUG_MAIL=true to "
                "print the body (it may contain a sign-in link)",
            )
        return True


class Smtp2goMailer:
    """The SMTP2GO HTTPS API. Never raises; a success is counted into the
    mail-allowance ledger (a rejected message consumed none of it)."""

    def __init__(
        self,
        settings: Settings,
        http: HttpClients,
        *,
        sessions: async_sessionmaker[AsyncSession],
        quota: QuotaCell,
        clock: Clock,
    ) -> None:
        self._settings = settings
        self._http = http
        self._sessions = sessions
        self._quota = quota
        self._clock = clock

    async def send(self, to: str, subject: str, body: str) -> bool:
        s = self._settings
        payload = {
            "sender": f"{s.mail_from_name} <{s.mail_from}>",
            "to": [to],
            "subject": subject,
            "text_body": body,
        }
        try:
            async with self._http.client(timeout=10.0) as client:
                resp = await client.post(
                    mail.API_URL,
                    json=payload,
                    headers={"X-Smtp2go-Api-Key": s.smtp2go_api_key},
                )
        except httpx.HTTPError:
            log.exception("mail.request_failed", to=to)
            return False
        ok = (
            resp.status_code == 200
            and resp.json().get("data", {}).get("succeeded", 0) >= 1
        )
        if not ok:
            log.error(
                "mail.send_failed", to=to, status=resp.status_code, body=resp.text[:500]
            )
            return False
        # Successes only: a rejected message consumed none of the allowance, and
        # a ledger that counted attempts would shout loudest when nothing sent.
        today = self._clock.now().astimezone(ZoneInfo(s.timezone)).date()
        await self._quota.record(self._sessions, today)
        return True


def default_mailer(
    settings: Settings,
    http: HttpClients,
    *,
    sessions: async_sessionmaker[AsyncSession],
    quota: QuotaCell,
    clock: Clock,
) -> Mailer:
    if not settings.smtp2go_api_key:
        return LoggingMailer(settings)
    return Smtp2goMailer(settings, http, sessions=sessions, quota=quota, clock=clock)
