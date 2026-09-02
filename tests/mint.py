"""What a test hands a service where the edge would hand its clock and its
random source: a moment, a token, a code, an invite."""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from volunteerdb.config import settings
from volunteerdb.env import SecretsRng
from volunteerdb.services.mail import MailContext
from volunteerdb.services.users import Invite

_rng = SecretsRng()


def now() -> datetime:
    return datetime.now(UTC)


def token() -> str:
    return _rng.token()


def code() -> str:
    return _rng.otp_code()


def fresh_invite(hours: int = 168, *, now: datetime | None = None) -> Invite:
    """An armed invite: minted at `now` (the test's clock, when it has one)
    with `hours` to live -- a short life lets one invite lapse while the
    others on the same page stay live."""
    return Invite(
        token=_rng.token(),
        now=now if now is not None else datetime.now(UTC),
        ttl=timedelta(hours=hours),
    )


def tz() -> ZoneInfo:
    return ZoneInfo(settings().timezone)


def uuid() -> UUID:
    return uuid4()


def today() -> date:
    return datetime.now(tz()).date()


def mail_context(org: str = "", invite_ttl_hours: int = 168) -> MailContext:
    return MailContext(org=org, invite_ttl_hours=invite_ttl_hours, tz=tz())
