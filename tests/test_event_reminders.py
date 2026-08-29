"""The nightly event digest: one email per person, the scheduled notice and
the two staged reminders (week / day-before) with their per-sign-up
preferences, the stamp-everything-satisfied rule, retry after a failed
send, and the exclusions (cancelled, past, self-signups)."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from volunteerdb.db import db_session
from volunteerdb.jobs import event_reminders
from volunteerdb.models import TeamRole
from volunteerdb.services import events as event_service
from volunteerdb.services import mail, memberships, teams, volunteers

from tests.fp_helpers import ok

TZ = ZoneInfo("America/Toronto")


@pytest.fixture
def sent_mail(monkeypatch):
    sent: list[tuple[str, str, str]] = []

    async def fake(to: str, subject: str, text_body: str) -> bool:
        sent.append((to, subject, text_body))
        return True

    monkeypatch.setattr(mail, "send_email", fake)
    return sent


def _at(days_ahead: int, hour: int) -> datetime:
    return datetime.combine(date.today() + timedelta(days=days_ahead), time(hour), TZ)


async def _team(n: int = 2) -> tuple[int, list[int]]:
    async with db_session() as session:
        team = ok(await teams.create(session, None, "Liturgy"))
        vids = []
        for i in range(n):
            v = await volunteers.create(
                session, None, f"Vol{i}", "Server", f"vol{i}@example.org"
            )
            await memberships.assign(
                session,
                None,
                v.id,
                team.id,
                TeamRole.leader if i == 0 else TeamRole.member,
            )
            vids.append(v.id)
        return team.id, vids


async def _event(team_id: int, days_ahead: int, title: str = "Mass") -> int:
    async with db_session() as session:
        created = await event_service.create_event(
            session,
            None,
            team_id=team_id,
            title=title,
            starts_at=_at(days_ahead, 10),
            ends_at=_at(days_ahead, 12),
            created_by=None,
        )
        return created[0].id


async def _assign(event_id: int, volunteer_id: int, *, notify_7d: bool = False) -> int:
    """Manager assignment, landing on the app's own defaults — week stage off.

    `assign` takes no preferences by design (the volunteer never chose), so a
    test that wants the week stage sets it on the row, which is what opting in
    later amounts to.
    """
    async with db_session() as session:
        view = await event_service.detail(session, None, event_id)
        a = await event_service.assign(
            session,
            None,
            slot_id=view.slots[0].slot.id,
            volunteer_id=volunteer_id,
            assigned_by=None,
        )
        if notify_7d:
            a.notify_7d = True
        return a.id


async def test_scheduled_notice_then_staged_reminders_then_silence(
    database, sent_mail, env
):
    """All three stages, for a volunteer who asked for all three."""
    team_id, vids = await _team()
    event_id = await _event(team_id, days_ahead=10)
    await _assign(event_id, vids[1], notify_7d=True)

    await event_reminders.main(env, today=date.today())
    assert len(sent_mail) == 1
    assert sent_mail[0][0] == "vol1@example.org"
    assert "You have been scheduled" in sent_mail[0][2]
    assert "Coming up" not in sent_mail[0][2]

    sent_mail.clear()
    await event_reminders.main(env, today=date.today())
    assert sent_mail == [], "the scheduled notice is one-shot"

    await event_reminders.main(env, today=date.today() + timedelta(days=2))
    assert sent_mail == [], "eight days out: no reminder window yet"

    await event_reminders.main(env, today=date.today() + timedelta(days=3))
    assert len(sent_mail) == 1
    assert "Coming up this week" in sent_mail[0][2]

    sent_mail.clear()
    await event_reminders.main(env, today=date.today() + timedelta(days=4))
    assert sent_mail == [], "the week reminder is one-shot"

    await event_reminders.main(env, today=date.today() + timedelta(days=9))
    assert len(sent_mail) == 1
    assert "Tomorrow — you are serving" in sent_mail[0][2]

    sent_mail.clear()
    await event_reminders.main(env, today=date.today() + timedelta(days=9))
    assert sent_mail == [], "the day reminder is one-shot too"


async def test_pending_everything_lists_once_and_stamps_every_stage(
    database, sent_mail, env
):
    team_id, vids = await _team()
    event_id = await _event(team_id, days_ahead=1)  # inside every window
    await _assign(event_id, vids[1])

    await event_reminders.main(env, today=date.today())
    assert len(sent_mail) == 1
    body = sent_mail[0][2]
    assert "You have been scheduled" in body
    assert body.count("Mass") == 1, "listed once, not once per notice"

    sent_mail.clear()
    await event_reminders.main(env, today=date.today())
    assert sent_mail == []
    await event_reminders.main(env, today=date.today() + timedelta(days=1))
    assert sent_mail == [], "every satisfied stage stamped — no repeats"


async def test_opted_out_stages_stay_silent(database, sent_mail, env):
    team_id, vids = await _team(3)
    event_id = await _event(team_id, days_ahead=5)
    async with db_session() as session:
        view = await event_service.detail(session, None, event_id)
        await event_service.sign_up(
            session,
            None,
            slot_id=view.slots[0].slot.id,
            volunteer_id=vids[1],
            notify_7d=False,
            notify_24h=True,
        )
        await event_service.sign_up(
            session,
            None,
            slot_id=view.slots[0].slot.id,
            volunteer_id=vids[2],
            notify_7d=True,
            notify_24h=False,
        )

    await event_reminders.main(env, today=date.today())
    assert [m[0] for m in sent_mail] == ["vol2@example.org"], (
        "vol1 opted out of the week stage; vol2 kept it"
    )
    assert "Coming up this week" in sent_mail[0][2]

    sent_mail.clear()
    await event_reminders.main(env, today=date.today() + timedelta(days=4))
    assert [m[0] for m in sent_mail] == ["vol1@example.org"], (
        "the day before: vol1's 24h stage fires; vol2 opted out of it"
    )
    assert "Tomorrow" in sent_mail[0][2]


async def test_one_email_per_person_covers_all_events(database, sent_mail, env):
    team_id, vids = await _team()
    for days in (5, 10):
        event_id = await _event(team_id, days_ahead=days, title=f"Mass+{days}")
        await _assign(event_id, vids[1])

    await event_reminders.main(env, today=date.today())
    assert len(sent_mail) == 1
    assert "Mass+5" in sent_mail[0][2] and "Mass+10" in sent_mail[0][2]


async def test_failed_send_retries_next_night(database, sent_mail, monkeypatch, env):
    team_id, vids = await _team()
    event_id = await _event(team_id, days_ahead=10)
    await _assign(event_id, vids[1])

    async def failing(to: str, subject: str, text_body: str) -> bool:
        return False

    monkeypatch.setattr(mail, "send_email", failing)
    await event_reminders.main(env, today=date.today())

    monkeypatch.setattr(
        mail,
        "send_email",
        lambda to, subject, text_body: _record(sent_mail, to, subject, text_body),
    )
    await event_reminders.main(env, today=date.today())
    assert len(sent_mail) == 1, "stamps stayed NULL, so the notice retried"


async def _record(sent, to, subject, body) -> bool:
    sent.append((to, subject, body))
    return True


async def test_exclusions(database, sent_mail, env):
    team_id, vids = await _team(3)

    cancelled_id = await _event(team_id, days_ahead=5, title="Cancelled Mass")
    await _assign(cancelled_id, vids[1])
    async with db_session() as session:
        await event_service.cancel_event(session, None, cancelled_id, cancelled_by=None)

    signup_id = await _event(team_id, days_ahead=10, title="Signup Mass")
    async with db_session() as session:
        view = await event_service.detail(session, None, signup_id)
        await event_service.sign_up(
            session,
            None,
            slot_id=view.slots[0].slot.id,
            volunteer_id=vids[2],
            notify_7d=True,  # opted in; off by default since the mail budget
        )

    await event_reminders.main(env, today=date.today())
    assert sent_mail == [], (
        "cancelled events are silent; self-signups get no scheduled notice"
    )

    # the self-signup still gets the week reminder later
    await event_reminders.main(env, today=date.today() + timedelta(days=3))
    assert [m[0] for m in sent_mail] == ["vol2@example.org"]
    assert "Coming up this week" in sent_mail[0][2]
    assert "You have been scheduled" not in sent_mail[0][2]


async def test_link_only_with_public_base_url(database, sent_mail, monkeypatch, env):
    team_id, vids = await _team()
    event_id = await _event(team_id, days_ahead=10)
    await _assign(event_id, vids[1])

    from volunteerdb import config

    patched = config.settings().model_copy(
        update={"public_base_url": "https://vdb.example.org"}
    )
    monkeypatch.setattr(event_reminders, "settings", lambda: patched)
    await event_reminders.main(env, today=date.today())
    assert "https://vdb.example.org/events" in sent_mail[0][2]


async def test_the_week_stage_is_off_unless_asked_for(database, sent_mail, env):
    """The default that keeps this instance inside its mail allowance.

    Three notices per assignment, batched one email a night, is what a weekend
    roster multiplies into ~1,300 messages a month against a 1,000 cap. The
    middle one restates the scheduling notice, so it now has to be asked for —
    and the two that carry new information still arrive on their own.
    """
    team_id, vids = await _team()
    event_id = await _event(team_id, days_ahead=10)
    await _assign(event_id, vids[1])  # no notify_7d: the app's default

    await event_reminders.main(env, today=date.today())
    assert len(sent_mail) == 1
    assert "You have been scheduled" in sent_mail[0][2]

    sent_mail.clear()
    await event_reminders.main(env, today=date.today() + timedelta(days=3))
    assert sent_mail == [], "seven days out, and silent: the week stage is opt-in"

    await event_reminders.main(env, today=date.today() + timedelta(days=9))
    assert len(sent_mail) == 1, "the notice that changes a day still arrives"
    assert "Tomorrow — you are serving" in sent_mail[0][2]
