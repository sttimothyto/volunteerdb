"""What a test hands a service where the edge would hand its clock and its
random source: a moment, a token, a code, an invite."""

from datetime import UTC, datetime, timedelta

from volunteerdb.auth import new_otp_code, new_token
from volunteerdb.services.users import Invite


def now() -> datetime:
    return datetime.now(UTC)


def token() -> str:
    return new_token()


def code() -> str:
    return new_otp_code()


def fresh_invite(hours: int = 168) -> Invite:
    return Invite(token=new_token(), now=now(), ttl=timedelta(hours=hours))
