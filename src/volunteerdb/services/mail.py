"""Outbound email via the SMTP2GO HTTPS API.

With no API key configured (local dev, tests, the verify harness) messages are
printed to stdout instead of sent, so OTP codes and invite links stay readable.
Callers should invoke send_email as a module attribute (``mail.send_email``)
so tests can monkeypatch it.
"""

from dataclasses import dataclass
from datetime import date

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


def ttl_window(hours: int) -> str:
    """'24 hours' / '7 days' — whole multiples of a day read as days."""
    if hours > 24 and hours % 24 == 0:
        return f"{hours // 24} days"
    return f"{hours} hours"


def invite_email(invite_url: str, ttl_hours: int | None = None) -> tuple[str, str]:
    hours = settings().invite_ttl_hours if ttl_hours is None else ttl_hours
    window = ttl_window(hours)
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


def interest_leader_email(
    team_path: str,
    name: str,
    email: str,
    phone: str | None,
    note: str | None,
    team_url: str,
) -> tuple[str, str]:
    """Sent to the team's leader(s) and second(s) when someone submits the
    public "I'm interested" form. The submitter's free text belongs here and
    only here — never in the applicant-facing mail."""
    lines = [f"Someone is interested in joining {team_path}:", ""]
    lines.append(f"  Name:  {name}")
    lines.append(f"  Email: {email}")
    if phone:
        lines.append(f"  Phone: {phone}")
    if note:
        lines.append(f"  Note:  {note}")
    lines += [
        "",
        "If the team has an application form linked, they were sent it "
        "directly; otherwise, please follow up with them.",
        f"Open interests are listed on your team page: {team_url}",
    ]
    return (f"New interest in {team_path}", "\n".join(lines))


def interest_applicant_email(
    team_name: str, application_form_url: str | None
) -> tuple[str, str]:
    """Confirmation to the address typed into the public form.

    A fixed template on purpose: the form is public and the recipient address
    is submitter-chosen, so echoing ANY submitted text (even the name) would
    let a stranger deliver their words to an arbitrary mailbox through the
    parish's sender. team_name comes from the database; the form URL is
    prefix-validated to Google Forms (services/teams.py)."""
    if application_form_url:
        next_step = (
            "The next step is the ministry's application form — "
            f"you can fill it in here: {application_form_url}"
        )
    else:
        next_step = (
            "The ministry leader will follow up with you about the next "
            "steps, including an application form."
        )
    return (
        f"Thank you for your interest in {team_name}",
        f"Thank you for your interest in the {team_name} ministry at "
        f"St. Timothy's — the ministry leaders have been told.\n\n"
        f"{next_step}\n\n"
        "If you didn't fill in a form on our ministries site, you can safely "
        "ignore this email.",
    )


@dataclass(frozen=True)
class DigestItem:
    """One proposal in a voter's nightly digest (jobs/proposal_digest.py)."""

    kind: str  # "added" | "voting" | "both"
    seat: str  # e.g. "Ministry leader — Liturgy / Music Ministry"
    nomination_deadline: date
    voting_deadline: date


_DIGEST_HEADERS = {
    "added": "You have been added to the voting roll for:",
    "voting": "Voting is now open for:",
    "both": "You have been added to the voting roll, and voting is already open, for:",
}


def proposal_digest_email(items: list[DigestItem]) -> tuple[str, str]:
    """One nightly email covering everything that changed for this voter —
    never one email per proposal. No hyperlink: the job runs in a cron
    container with no request and no configured public URL."""
    sections = []
    for kind, header in _DIGEST_HEADERS.items():
        lines = [
            f"  • {item.seat}\n"
            f"    (nomination deadline {item.nomination_deadline:%B %-d, %Y}, "
            f"voting deadline {item.voting_deadline:%B %-d, %Y})"
            for item in items
            if item.kind == kind
        ]
        if lines:
            sections.append(header + "\n" + "\n".join(lines))
    body = (
        "\n\n".join(sections)
        + "\n\nSign in to VolunteerDB and open the Planning page to nominate "
        "candidates or cast your ballot. Deadlines are inclusive — you can "
        "act through the end of the deadline day."
    )
    return ("VolunteerDB planning: your input is needed", body)


def welcome_email(login_url: str, has_password: bool) -> tuple[str, str]:
    how = (
        f"Sign in at {login_url} with your email and password."
        if has_password
        else f"No password needed — sign in at {login_url} with just your email, "
        "and we'll send a one-time code to this address each time."
    )
    return ("Your VolunteerDB account is ready", f"You're all set. {how}")
