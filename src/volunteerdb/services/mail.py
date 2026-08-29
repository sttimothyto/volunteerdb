"""Outbound email: the parish copy, as pure templates.

Every builder returns (subject, body) for the values it is handed; the
parish's name and zone arrive in a MailContext. The transport lives at the
edge (env.Smtp2goMailer; env.LoggingMailer without an API key, which prints
messages instead so OTP codes and invite links stay readable in dev).
Callers still invoke ``mail.send_email`` as a module attribute so tests can
monkeypatch it.

Every message that actually leaves is counted into `mail_quota` (see
services/mail_quota.py): the free tier allows 200 a day and 1,000 a month, and
the app has no way to notice it has run out except by a send failing. That
ledger is also the reason the copy below batches so hard — one digest a night
per person, never one message per event.
"""

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

API_URL = "https://api.smtp2go.com/v3/email/send"


@dataclass(frozen=True, slots=True)
class MailContext:
    """What the parish copy needs from the settings: its name (VDB_ORG_NAME,
    empty when unset -- every sentence below reads either way), how long an
    invite link lives, and the parish zone times are written in.
    env.Env.mail_context() builds one."""

    org: str
    invite_ttl_hours: int
    tz: ZoneInfo


async def send_email(to: str, subject: str, text_body: str) -> bool:
    """Send one message; True on success. Never raises.

    Transition: the transport is the running Env's Mailer (env.Smtp2goMailer,
    or env.LoggingMailer without an API key); this name stays so the edges and
    the tests' monkeypatches keep working until the edges hand SendMail
    effects to the interpreter (FUNCTIONAL_REFACTORING.md, Phase 4)."""
    from ..env import current

    return await current().mailer.send(to, subject, text_body)


def ttl_window(hours: int) -> str:
    """'24 hours' / '7 days' — whole multiples of a day read as days."""
    if hours > 24 and hours % 24 == 0:
        return f"{hours // 24} days"
    return f"{hours} hours"


def org(ctx: MailContext) -> str:
    """The parish this instance serves (VDB_ORG_NAME), or "" if unset.

    Every use below reads as a complete sentence either way. The phrasing
    deliberately avoids a possessive — "St. Timothy's's volunteer database" is
    otherwise unavoidable for any name ending in s.
    """
    return ctx.org.strip()


def invite_email(
    invite_url: str, ctx: MailContext, ttl_hours: int | None = None
) -> tuple[str, str]:
    hours = ctx.invite_ttl_hours if ttl_hours is None else ttl_hours
    window = ttl_window(hours)
    where = org(ctx)
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
    confirm_url: str, new_address: str, ttl_hours: int, ctx: MailContext
) -> tuple[str, str]:
    """Sent to the address somebody wants to move an account to.

    The message is not a notice, it is the proof: until this link is opened
    the address is only a claim, and a mistyped one lands in a stranger's
    mailbox, where all it offers is the chance to decline. So it names the
    address it would set, says what it would move, and gives an unmistakable
    "not you? ignore this". The address being *replaced* hears separately —
    see email_change_requested_email."""
    where = org(ctx)
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


def address_edited_email(
    new_address: str, login_url: str | None = None
) -> tuple[str, str]:
    """Sent to the address a *leader or admin* has just moved a volunteer away
    from — the third-party counterpart to email_change_done_email.

    Changing somebody else's address applies immediately, deliberately: leaders
    fix bounced addresses precisely because the volunteer cannot read the mail
    at the old one. That same immediacy is what makes it a takeover step —
    point a volunteer's address at your own, ask for their invite, redeem it.
    The independent-channel notice of SP 800-63B §4.1.2 is what turns a silent
    redirect into one the volunteer can see and report, so it goes out even
    though the old address is often the broken one; a bounce costs nothing.

    One deliberate edit, one message. The importer and the roster-sheet sync
    used to send this in bulk too, and a single cleanup pass over a messy sheet
    could fire dozens at once — mostly at the dead addresses it was fixing.
    That path is gone: a sheet redirect is still recorded (ImportReport
    .addresses_replaced, and volunteer_history), it is simply not mailed."""
    sign_in = f" — sign in at {login_url}" if login_url else ""
    return (
        "Your VolunteerDB address was changed",
        f"Somebody who helps run one of your ministries changed the address on "
        f"your VolunteerDB record to {new_address}. That is now the address "
        "used for rosters, event notices, and signing in.\n\n"
        "This is the last message we will send to this address.\n\n"
        "If you asked for that, or you knew about it, there is nothing to "
        f"do{sign_in}.\n\n"
        "If it is news to you, tell the parish office straight away.",
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


def event_when(starts_at: datetime, ends_at: datetime, tz: ZoneInfo) -> str:
    """'Sunday, August 23, 2026, 10:30 AM–12:00 PM' in the parish's clock."""
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
    items: list[EventDigestItem], events_url: str | None = None, *, tz: ZoneInfo
) -> tuple[str, str]:
    """One nightly email covering all of this volunteer's event notices —
    never one email per event. events_url is VDB_PUBLIC_BASE_URL via
    the job; unset omits the link (the proposal-digest precedent)."""
    sections = []
    for kind, header in _EVENT_DIGEST_HEADERS.items():
        lines = []
        for item in (i for i in items if i.kind == kind):
            where = f", {item.location}" if item.location else ""
            lines.append(
                f"  • {item.title} — {item.slot} ({item.path})\n"
                f"    {event_when(item.starts_at, item.ends_at, tz)}{where}"
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
    appear verbatim."""
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
    """Sent to the person who asked, once a substitution is claimed.

    The team's leaders used to be copied. They were the only recipients with
    nothing to do — the message tells them the gap they never had to fill has
    been filled — and on a team with three leaders that was four messages
    where one was actionable. The roster on the event page is the same fact,
    live, for anyone who wants it."""
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
