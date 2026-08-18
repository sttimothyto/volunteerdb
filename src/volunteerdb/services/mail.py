"""Outbound email via the SMTP2GO HTTPS API.

With no API key configured (local dev, tests, the verify harness) messages are
printed to stdout instead of sent, so OTP codes and invite links stay readable.
Callers should invoke send_email as a module attribute (``mail.send_email``)
so tests can monkeypatch it.
"""

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

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


def org() -> str:
    """The parish this instance serves (VDB_ORG_NAME), or "" if unset.

    Every use below reads as a complete sentence either way. The phrasing
    deliberately avoids a possessive — "St. Timothy's's volunteer database" is
    otherwise unavoidable for any name ending in s.
    """
    return settings().org_name.strip()


def invite_email(invite_url: str, ttl_hours: int | None = None) -> tuple[str, str]:
    hours = settings().invite_ttl_hours if ttl_hours is None else ttl_hours
    window = ttl_window(hours)
    where = org()
    subject = "Your VolunteerDB account"
    if where:
        subject += f" at {where}"
    opening = (
        f"An account has been created for you in VolunteerDB, the volunteer "
        f"database for {where}."
        if where
        else "An account has been created for you in VolunteerDB, the volunteer "
        "database."
    )
    return (
        subject,
        f"{opening}\n\n"
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


def email_change_email(
    confirm_url: str, new_address: str, ttl_hours: int
) -> tuple[str, str]:
    """Sent to the address somebody wants to move an account to.

    The message is not a notice, it is the proof: until this link is opened
    the address is only a claim, and a mistyped one lands in a stranger's
    mailbox, where all it offers is the chance to decline. So it names the
    address it would set, says what it would move, and gives an unmistakable
    "not you? ignore this". The address being *replaced* hears separately —
    see email_change_requested_email."""
    where = org()
    subject = "Confirm your new VolunteerDB address"
    what = (
        f"VolunteerDB, the volunteer database for {where}"
        if where
        else "VolunteerDB, the volunteer database"
    )
    return (
        subject,
        f"Somebody asked to change the address on an account in {what}, "
        f"to this one ({new_address}).\n\n"
        f"To confirm it, open this link: {confirm_url}\n\n"
        "Until then nothing changes: the account still signs in at its old "
        "address, and that is where the app still writes. Once confirmed, "
        "this address becomes both the sign-in address and the contact "
        "address on every ministry roster the volunteer serves on.\n\n"
        f"The link works once, and only for the next {ttl_window(ttl_hours)}.\n\n"
        "If you were not expecting this, ignore this email — without the link "
        "nothing happens, and nobody learns whether this address exists.",
    )


def email_change_requested_email(
    new_address: str, account_url: str, ttl_hours: int
) -> tuple[str, str]:
    """Sent to the address an account is being moved *away* from, the moment
    somebody asks — the counterpart to email_change_email above.

    NIST SP 800-63B §4.1.2: "the CSP SHALL notify the subscriber via a
    mechanism independent of the transaction". A session someone else is
    driving cannot suppress what lands in the mailbox this account is
    currently reachable at, and that is the only thing standing between a
    hijacked session and a silent, permanent takeover — the address is the
    credential on the emailed-code path, so moving it is moving the account.

    It goes at *request* time, not at confirmation, because only then is it
    still actionable: the old address still signs in, and the change can be
    called off from /account before the link is ever opened."""
    return (
        "Your VolunteerDB address is being changed",
        f"Somebody asked to change the address on your VolunteerDB account to "
        f"{new_address}, and we have sent a confirmation link there.\n\n"
        "Nothing has changed yet. You still sign in at this address, and the "
        f"request expires by itself in {ttl_window(ttl_hours)} if the link is "
        "never opened.\n\n"
        f"If that was you, there is nothing to do.\n\n"
        f"If it was not, sign in at {account_url} and cancel it — then tell "
        "the parish office, because somebody else has access to this account.",
    )


def email_change_done_email(new_address: str, login_url: str) -> tuple[str, str]:
    """Sent to the address an account has just moved away from: the last
    message this mailbox gets from the app, and the receipt §4.1.2 asks for at
    the moment the binding actually changes (the shape password_changed_email
    already follows)."""
    return (
        "Your VolunteerDB address changed",
        f"The address on your VolunteerDB account is now {new_address}. From "
        "now on that is the address you sign in with, and the one on every "
        "ministry roster you serve on.\n\n"
        "This is the last message we will send to this address.\n\n"
        f"If that was you, there is nothing to do — sign in at {login_url}.\n\n"
        "If it was not, tell the parish office straight away: somebody else "
        "has taken over this account.",
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
    where = org()
    at_org = f" at {where}" if where else ""
    return (
        f"Thank you for your interest in {team_name}",
        f"Thank you for your interest in the {team_name} ministry{at_org}"
        " — the ministry leaders have been told.\n\n"
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
        + "\n\nSign in to VolunteerDB and open the Elections page to nominate "
        "candidates or cast your ballot. Deadlines are inclusive — you can "
        "act through the end of the deadline day."
    )
    return ("VolunteerDB elections: your input is needed", body)


# --- events -------------------------------------------------------------------


def event_when(starts_at: datetime, ends_at: datetime) -> str:
    """'Sunday, August 23, 2026, 10:30 AM–12:00 PM' in the parish's clock."""
    tz = ZoneInfo(settings().timezone)
    s, e = starts_at.astimezone(tz), ends_at.astimezone(tz)
    if e.date() == s.date():
        return f"{s:%A, %B %-d, %Y}, {s:%-I:%M %p}–{e:%-I:%M %p}"
    return f"{s:%A, %B %-d, %Y}, {s:%-I:%M %p} – {e:%A, %B %-d, %Y}, {e:%-I:%M %p}"


@dataclass(frozen=True)
class EventDigestItem:
    """One assignment in a volunteer's nightly events digest
    (jobs/event_reminders.py)."""

    kind: str  # "scheduled" | "week" | "day"
    title: str
    path: str  # team path, e.g. "Liturgy / Lectors"
    slot: str
    starts_at: datetime
    ends_at: datetime
    location: str | None = None


# dict order is section order in the email: strongest notice first
_EVENT_DIGEST_HEADERS = {
    "scheduled": "You have been scheduled to serve:",
    "day": "Tomorrow — you are serving:",
    "week": "Coming up this week — you are serving:",
}


def event_digest_email(
    items: list[EventDigestItem], events_url: str | None = None
) -> tuple[str, str]:
    """One nightly email covering all of this volunteer's event notices —
    never one email per event. events_url is settings().public_base_url via
    the job; unset omits the link (the proposal-digest precedent)."""
    sections = []
    for kind, header in _EVENT_DIGEST_HEADERS.items():
        lines = []
        for item in (i for i in items if i.kind == kind):
            where = f", {item.location}" if item.location else ""
            lines.append(
                f"  • {item.title} — {item.slot} ({item.path})\n"
                f"    {event_when(item.starts_at, item.ends_at)}{where}"
            )
        if lines:
            sections.append(header + "\n" + "\n".join(lines))
    tail = (
        "\n\nIf you can no longer serve, open the Events page in VolunteerDB "
        "and request a substitute so a teammate can take the slot."
    )
    if events_url:
        tail += f"\nYour events: {events_url}"
    return ("VolunteerDB: your upcoming service", "\n\n".join(sections) + tail)


def sub_request_email(
    title: str,
    path: str,
    slot: str,
    when: str,
    requester_name: str,
    note: str | None,
    events_url: str,
) -> tuple[str, str]:
    """Sent to the event team's members (minus the requester and anyone
    already serving that event) when someone asks for a substitute. The
    audience is authenticated teammates, so the requester's short note may
    appear — unlike the public interest form's mail."""
    lines = [
        f"{requester_name} can no longer serve as {slot} at:",
        "",
        f"  {title} — {path}",
        f"  {when}",
    ]
    if note:
        lines += ["", f"Their note: {note}"]
    lines += [
        "",
        "The first teammate to claim the slot takes it — open the Events "
        f"page to help out: {events_url}",
    ]
    return (f"Substitute needed: {title}", "\n".join(lines))


def sub_claimed_email(
    title: str, slot: str, when: str, claimant_name: str, asker_name: str
) -> tuple[str, str]:
    """Sent to the person who asked AND the team's leaders once a
    substitution is claimed."""
    return (
        f"Substitute found: {title}",
        f"{claimant_name} has taken over {asker_name}'s {slot} slot at "
        f"{title} ({when}).\n\n"
        "Nothing more to do — the schedule has been updated.",
    )


def substituted_in_email(
    title: str, slot: str, when: str, outgoing_name: str, events_url: str
) -> tuple[str, str]:
    """Sent to the incoming volunteer when a teammate hands them the slot
    directly (no open call)."""
    return (
        f"You're now serving: {title}",
        f"{outgoing_name} has handed you their {slot} slot at {title} "
        f"({when}).\n\n"
        "The schedule has been updated — if this doesn't work for you, open "
        f"the Events page to pass it on or ask for a substitute: {events_url}",
    )


def self_removal_email(
    title: str, path: str, slot: str, when: str, volunteer_name: str, reason: str
) -> tuple[str, str]:
    """Sent to the team's leaders when somebody takes themselves off a slot.
    The reason is required in the dialog and quoted verbatim — the audience
    is the leadership, the same trust boundary as the sub-request note."""
    return (
        f"Off the roster: {title}",
        f"{volunteer_name} has taken themselves off the {slot} slot at:\n\n"
        f"  {title} — {path}\n"
        f"  {when}\n\n"
        f"Their reason: {reason}\n\n"
        "The slot is open again — assign someone from the event page if it "
        "needs filling.",
    )


def event_cancelled_email(title: str, path: str, when: str) -> tuple[str, str]:
    """Sent to every assignee when a manager cancels an upcoming event."""
    return (
        f"Cancelled: {title}",
        f"The event {title} ({path}), scheduled for {when}, has been "
        "cancelled.\n\n"
        "You were signed up to serve there; no action is needed.",
    )


def welcome_email(login_url: str, has_password: bool) -> tuple[str, str]:
    how = (
        f"Sign in at {login_url} with your email and password."
        if has_password
        else f"No password needed — sign in at {login_url} with just your email, "
        "and we'll send a one-time code to this address each time."
    )
    return ("Your VolunteerDB account is ready", f"You're all set. {how}")
