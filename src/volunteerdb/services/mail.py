"""Outbound email via the SMTP2GO HTTPS API.

With no API key configured (local dev, tests, the verify harness) messages are
printed to stdout instead of sent, so OTP codes and invite links stay readable.
Callers should invoke send_email as a module attribute (``mail.send_email``)
so tests can monkeypatch it.
"""

import logging

import httpx

from ..config import settings

API_URL = "https://api.smtp2go.com/v3/email/send"

log = logging.getLogger(__name__)


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
        log.exception("smtp2go request failed (to=%s)", to)
        return False
    ok = resp.status_code == 200 and resp.json().get("data", {}).get("succeeded", 0) >= 1
    if not ok:
        log.error("smtp2go send failed (to=%s): %s %s", to, resp.status_code, resp.text[:500])
    return ok


def invite_email(invite_url: str) -> tuple[str, str]:
    return (
        "Your VolunteerDB account at St. Timothy's",
        "An account has been created for you in VolunteerDB, St. Timothy's "
        "volunteer database.\n\n"
        f"Finish setting it up here: {invite_url}\n\n"
        "Setting a password is optional. If you skip it, we'll email you a "
        "one-time sign-in code each time you log in.\n\n"
        "This link can only be used once; if it stops working, ask a parish "
        "admin for a new one.",
    )


def otp_email(code: str) -> tuple[str, str]:
    return (
        f"Your VolunteerDB sign-in code: {code}",
        f"Your one-time sign-in code is {code}. It expires in 10 minutes.\n\n"
        "If you didn't try to sign in, you can ignore this email — your "
        "account is safe.",
    )


def welcome_email(login_url: str, has_password: bool) -> tuple[str, str]:
    how = (
        f"Sign in at {login_url} with your email and password."
        if has_password
        else f"No password needed — sign in at {login_url} with just your email, "
        "and we'll send a one-time code to this address each time."
    )
    return ("Your VolunteerDB account is ready", f"You're all set. {how}")
