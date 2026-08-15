"""Outbound email via the SMTP2GO HTTPS API.

With no API key configured (local dev, tests, the verify harness) messages are
printed to stdout instead of sent, so OTP codes and invite links stay readable.
Callers should invoke send_email as a module attribute (``mail.send_email``)
so tests can monkeypatch it.
"""

import httpx
import structlog

from ..config import settings

API_URL = "https://api.smtp2go.com/v3/email/send"

log = structlog.get_logger(__name__)


async def send_email(to: str, subject: str, text_body: str) -> bool:
    """Send one message; True on success. Never raises."""
    s = settings()
    if not s.smtp2go_api_key:
        print(f"[MAIL] to={to} subject={subject!r}\n{text_body}", flush=True)
        return True
    payload = {
        "sender": f"{s.mail_from_name} <{s.mail_from}>",
        "to": [to],
        "subject": subject,
        "text_body": text_body,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                API_URL, json=payload, headers={"X-Smtp2go-Api-Key": s.smtp2go_api_key}
            )
    except httpx.HTTPError:
        log.exception("mail.request_failed", to=to)
        return False
    ok = (
        resp.status_code == 200 and resp.json().get("data", {}).get("succeeded", 0) >= 1
    )
    if not ok:
        log.error(
            "mail.send_failed", to=to, status=resp.status_code, body=resp.text[:500]
        )
    return ok


def invite_email(invite_url: str, ttl_hours: int | None = None) -> tuple[str, str]:
    hours = settings().invite_ttl_hours if ttl_hours is None else ttl_hours
    window = "24 hours" if hours == 24 else f"{hours} hours"
    return (
        "Your VolunteerDB account at St. Timothy's",
        "An account has been created for you in VolunteerDB, St. Timothy's "
        "volunteer database.\n\n"
        f"Finish setting it up here: {invite_url}\n\n"
        "Setting a password is optional. If you skip it, we'll email you a "
        "one-time sign-in code each time you log in.\n\n"
        f"The link works once, and only for the next {window}. If it has run "
        "out, you can still sign in at any time by entering your email address "
        "with the password field left blank — we'll email you a code.",
    )


def otp_email(code: str) -> tuple[str, str]:
    return (
        f"Your VolunteerDB sign-in code: {code}",
        f"Your one-time sign-in code is {code}. It expires in 10 minutes.\n\n"
        "If you didn't try to sign in, you can ignore this email — your "
        "account is safe.",
    )


def password_changed_email(login_url: str, *, removed: bool = False) -> tuple[str, str]:
    """Sent to the account's own address whenever its password changes.

    NIST SP 800-63B §4.1.2: "When an authenticator is added, the CSP SHALL
    notify the subscriber via a mechanism independent of the transaction
    binding the new authenticator" — independent because a session someone
    else is driving cannot suppress what lands in the volunteer's mailbox."""
    what = (
        "The password on your VolunteerDB account was removed. You now sign in "
        "with a one-time code emailed to this address."
        if removed
        else "The password on your VolunteerDB account was just changed."
    )
    return (
        "Your VolunteerDB password changed",
        f"{what}\n\n"
        f"If that was you, there is nothing to do — sign in at {login_url}.\n\n"
        "If it was not, tell the parish office straight away: somebody else "
        "has access to this account.",
    )


def welcome_email(login_url: str, has_password: bool) -> tuple[str, str]:
    how = (
        f"Sign in at {login_url} with your email and password."
        if has_password
        else f"No password needed — sign in at {login_url} with just your email, "
        "and we'll send a one-time code to this address each time."
    )
    return ("Your VolunteerDB account is ready", f"You're all set. {how}")
