"""The events pages: navbar entry, member scoping, RSVP, sign-up, the
substitution flow with its emails, event creation via the dialog, and the
attendance section on past events."""

from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import events as event_service
from volunteerdb.services import mail, memberships, teams, users, volunteers

SIM_MAIN = Path(__file__).parent / "ui_sim_main.py"
TZ = ZoneInfo("America/Toronto")


@pytest.fixture
def sent_mail(monkeypatch):
    sent: list[tuple[str, str, str]] = []

    async def fake(to: str, subject: str, text_body: str) -> bool:
        sent.append((to, subject, text_body))
        return True

    monkeypatch.setattr(mail, "send_email", fake)
    return sent


async def _parish(session):
    """Liturgy (Lena leads, Mia + Noor members) and Choir (Oda member)."""
    liturgy = await teams.create(session, "Liturgy")
    choir = await teams.create(session, "Choir")
    lena = await volunteers.create(session, "Lena", "Leader", "lena@example.org")
    mia = await volunteers.create(session, "Mia", "Member", "mia@example.org")
    noor = await volunteers.create(session, "Noor", "Member", "noor@example.org")
    oda = await volunteers.create(session, "Oda", "Chorister", "oda@example.org")
    await memberships.assign(session, lena.id, liturgy.id, TeamRole.leader)
    await memberships.assign(session, mia.id, liturgy.id, TeamRole.member)
    await memberships.assign(session, noor.id, liturgy.id, TeamRole.member)
    await memberships.assign(session, oda.id, choir.id, TeamRole.member)
    lena_u, _ = await users.create(session, "lena@example.org", volunteer_id=lena.id)
    mia_u, _ = await users.create(session, "mia@example.org", volunteer_id=mia.id)
    noor_u, _ = await users.create(session, "noor@example.org", volunteer_id=noor.id)
    oda_u, _ = await users.create(session, "oda@example.org", volunteer_id=oda.id)
    return {
        "liturgy": liturgy.id,
        "choir": choir.id,
        "lena": lena.id,
        "mia": mia.id,
        "noor": noor.id,
        "lena_u": lena_u.id,
        "mia_u": mia_u.id,
        "noor_u": noor_u.id,
        "oda_u": oda_u.id,
    }


def _next_week(hour: int) -> datetime:
    return datetime.combine(date.today() + timedelta(days=7), time(hour), TZ)


async def _seed_event(team_id: int, *, slots=None, title="Sunday Mass") -> int:
    async with db_session() as session:
        created = await event_service.create_event(
            session,
            team_id=team_id,
            title=title,
            starts_at=_next_week(10),
            ends_at=_next_week(12),
            slots=slots,
            created_by=None,
        )
        return created[0].id


async def test_member_scoping_and_rsvp(database):
    async with db_session() as session:
        ids = await _parish(session)
    event_id = await _seed_event(ids["liturgy"])
    choir_event = await _seed_event(ids["choir"], title="Choir practice")

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.should_see("dev-login ok")
        await user.open("/events")
        await user.should_see("Events")  # navbar + page title
        # table contents live in the rows prop, not element text
        table = user.find(kind=ui.table).elements.pop()
        assert [r["title"] for r in table.rows] == ["Sunday Mass"], (
            "only own-team events; Choir practice stays hidden"
        )

        await user.open(f"/events/{choir_event}")
        await user.should_see("visible to the members of its team")

        await user.open(f"/events/{event_id}")
        await user.should_see("Can you serve at this event?")
        user.find("Available", kind=ui.button).click()
        await user.should_see("you said: available", retries=30)


async def test_signup_withdraw_and_leader_assign(database):
    async with db_session() as session:
        ids = await _parish(session)
    event_id = await _seed_event(
        ids["liturgy"], slots=[event_service.SlotInput("Lector", 2)]
    )

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open(f"/events/{event_id}")
        user.find("Sign up", kind=ui.button).click()  # opens the dialog
        user.find(marker="signup-confirm").click()
        await user.should_see("Mia Member", retries=30)
        await user.should_see("1/2")
        # withdrawing asks for a reason (mailed to the leaders) since Phase 4
        user.find("Withdraw", kind=ui.button).click()
        await user.should_see("Why can you no longer serve?")
        box = user.find(kind=ui.textarea).elements.pop()
        box.value = "schedule conflict"
        user.find("Take me off", kind=ui.button).click()
        await user.should_see("0/2", retries=30)

        # the leader schedules Noor from the picker
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/events/{event_id}")
        pick = user.find(kind=ui.select, content="Schedule someone").elements.pop()
        pick.value = ids["noor"]
        user.find("Assign", kind=ui.button).click()
        await user.should_see("Noor Member", retries=30)
        await user.should_see("1/2")


async def test_sub_request_claim_flow_with_mail(database, sent_mail):
    async with db_session() as session:
        ids = await _parish(session)
    event_id = await _seed_event(ids["liturgy"])
    async with db_session() as session:
        view = await event_service.detail(session, event_id)
        a = await event_service.sign_up(
            session, slot_id=view.slots[0].slot.id, volunteer_id=ids["mia"]
        )
        assert a.volunteer_id == ids["mia"]

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open("/events")
        await user.should_see("Your upcoming duties")
        user.find("Need a sub", kind=ui.button).click()
        await user.should_see("Ask for a substitute")
        note = user.find(kind=ui.input, content="Note to the team").elements.pop()
        note.value = "out of town"
        user.find("Ask the team", kind=ui.button).click()
        await user.should_see("sub wanted", retries=30)

        # the mail went to teammates who are not already serving: Lena + Noor
        assert {m[0] for m in sent_mail} == {"lena@example.org", "noor@example.org"}
        assert all("Substitute needed" in m[1] for m in sent_mail)
        assert "out of town" in sent_mail[0][2]
        sent_mail.clear()

        # Noor sees and claims it
        await user.open(f"/login-dev/{ids['noor_u']}")
        await user.open("/events")
        await user.should_see("Teammates need a substitute")
        await user.should_see("out of town")
        user.find("Take this slot", kind=ui.button).click()
        await user.should_see("The slot is yours", retries=30)

        # asker + leader are told
        assert {m[0] for m in sent_mail} == {"mia@example.org", "lena@example.org"}
        assert all("Substitute found" in m[1] for m in sent_mail)
        assert "Noor Member" in sent_mail[0][2]

        await user.open(f"/events/{event_id}")
        await user.should_see("Noor Member")
        await user.should_see("substitute")


async def test_leader_creates_event_via_dialog(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open("/events")
        user.find("New event", kind=ui.button).click()
        await user.should_see("Repeat weekly until")
        team = user.find(kind=ui.select, content="Team").elements.pop()
        team.value = ids["liturgy"]
        title = user.find(kind=ui.input, content="Title").elements.pop()
        title.value = "Parish picnic"
        repeat = user.find(kind=ui.input, content="Repeat weekly until").elements.pop()
        repeat.value = str(date.today() + timedelta(days=8))
        # NB: waiting on "Parish picnic" would match the dialog's own title
        # input instantly; the 0/∞ badge exists only on the detail page
        user.find("Create event", kind=ui.button).click()
        await user.should_see("0/∞", retries=50)

    async with db_session() as session:
        admin, _ = await users.create(session, "admin@example.org", is_admin=True)
        from volunteerdb.permissions import load_actor

        actor = await load_actor(session, admin)
        picnics = [
            s
            for s in await event_service.list_events(session, actor)
            if s.event.title == "Parish picnic"
        ]
        assert len(picnics) == 2, "day 1 and day 8"


async def test_calendar_embed_follows_configuration(database, monkeypatch):
    from volunteerdb.config import settings
    from volunteerdb.services import gcal

    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open("/events")
        await user.should_not_see(marker="gcal-embed")

    cfg = settings().model_copy(
        update={"gcal_calendar_id": "parish@group.calendar.google.com"}
    )
    monkeypatch.setattr(gcal, "settings", lambda: cfg)
    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open("/events")
        await user.should_see(marker="gcal-embed")
        await user.open("/events?past=1")
        await user.should_not_see(marker="gcal-embed")


async def test_events_table_search_sort_and_column_drag(database):
    async with db_session() as session:
        ids = await _parish(session)
    await _seed_event(ids["liturgy"], title="Sunday Mass")
    await _seed_event(ids["liturgy"], title="Bake sale")

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open("/events")
        table = user.find(kind=ui.table).elements.pop()
        assert {r["title"] for r in table.rows} == {"Sunday Mass", "Bake sale"}
        assert all(c.get("sortable") for c in table.columns)
        await user.should_see("2 events")

        search = user.find(kind=ui.input, content="Search events").elements.pop()
        search.value = "bake"
        assert [r["title"] for r in table.rows] == ["Bake sale"]
        await user.should_see("1 of 2 events")

        search.value = "title ILIKE '%mass%'"
        assert [r["title"] for r in table.rows] == ["Sunday Mass"]

        search.value = "nonsense = 'field'"
        await user.should_see("query error")

        search.value = ""
        assert len(table.rows) == 2

        # a header drop reorders the columns there and then (leftwards drop
        # lands before the target — the column_order.reorder contract)
        user.find(kind=ui.table).trigger(
            "vdbColMove", {"moved": "team", "target": "title"}
        )
        table = user.find(kind=ui.table).elements.pop()
        assert [c["name"] for c in table.columns][:3] == ["when", "team", "title"]


async def test_share_button_and_date_pickers(database):
    async with db_session() as session:
        ids = await _parish(session)
    event_id = await _seed_event(ids["liturgy"])

    async with user_simulation(main_file=SIM_MAIN) as user:
        # any viewer can share; the dialog carries the accounts caveat
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open(f"/events/{event_id}")
        user.find(marker="share-event").click()
        await user.should_see("Event link copied")
        await user.should_see("make sure every")
        url_box = user.find(kind=ui.input, content=f"/events/{event_id}")
        assert url_box.elements, "the copied link is shown for hand-copying"

        # both date fields of the create dialog carry popup calendars
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open("/events")
        user.find("New event", kind=ui.button).click()
        await user.should_see("Repeat weekly until")
        assert len(user.find(kind=ui.date).elements) == 2


async def test_collaboration_card_lets_another_team_staff_the_event(database):
    async with db_session() as session:
        ids = await _parish(session)
    event_id = await _seed_event(ids["liturgy"])

    async with user_simulation(main_file=SIM_MAIN) as user:
        # Oda (Choir) cannot even view the Liturgy event yet
        await user.open(f"/login-dev/{ids['oda_u']}")
        await user.open(f"/events/{event_id}")
        await user.should_see("visible to the members of its team")

        # Lena adds the Choir as a collaborating team
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/events/{event_id}")
        await user.should_see("Collaboration")
        pick = user.find(
            kind=ui.select, content="Add collaborating team"
        ).elements.pop()
        pick.value = ids["choir"]
        user.find(marker="add-collaborator").click()
        await user.should_see(marker="confirm-collaborator", retries=30)
        await user.should_see("co-manage the event")
        user.find(marker="confirm-collaborator").click()
        await user.should_see("their roster can sign up now", retries=30)

        # Oda now sees the event and can take a slot
        await user.open(f"/login-dev/{ids['oda_u']}")
        await user.open(f"/events/{event_id}")
        user.find("Sign up", kind=ui.button).click()
        user.find(marker="signup-confirm").click()
        await user.should_see("Oda Chorister", retries=30)


async def test_signup_notification_prefs_persist(database):
    async with db_session() as session:
        ids = await _parish(session)
    event_id = await _seed_event(ids["liturgy"])

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open(f"/events/{event_id}")
        user.find("Sign up", kind=ui.button).click()
        await user.should_see("Email me a reminder")
        week = user.find(kind=ui.checkbox, content="7 days").elements.pop()
        day = user.find(kind=ui.checkbox, content="24 hours").elements.pop()
        assert week.value and day.value, "both stages pre-checked"
        day.value = False
        user.find(marker="signup-confirm").click()
        await user.should_see("You're on the list", retries=30)

    async with db_session() as session:
        view = await event_service.detail(session, event_id)
        assignment = view.slots[0].entries[0][0]
        assert (assignment.notify_7d, assignment.notify_24h) == (True, False)


async def test_series_signup_repeats_across_weeks(database):
    async with db_session() as session:
        ids = await _parish(session)
    start = _next_week(10)
    async with db_session() as session:
        weeks = await event_service.create_event(
            session,
            team_id=ids["liturgy"],
            title="Sunday Mass",
            starts_at=start,
            ends_at=_next_week(12),
            repeat_weekly_until=start.date() + timedelta(days=14),
            created_by=None,
        )
        week_ids = [e.id for e in weeks]
    assert len(week_ids) == 3

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open(f"/events/{week_ids[0]}")
        user.find("Sign up", kind=ui.button).click()
        await user.should_see("later weeks of this series")
        box = user.find(kind=ui.checkbox, content="later weeks").elements.pop()
        box.value = True
        user.find(marker="signup-confirm").click()
        await user.should_see("this week plus 2 more", retries=30)

    async with db_session() as session:
        for week_id in week_ids:
            d = await event_service.detail(session, week_id)
            assert any(v.id == ids["mia"] for sv in d.slots for _, v in sv.entries)


async def test_handoff_and_self_removal_flows_with_mail(database, sent_mail):
    async with db_session() as session:
        ids = await _parish(session)
    event_id = await _seed_event(
        ids["liturgy"], slots=[event_service.SlotInput("Lector", 2)]
    )
    async with db_session() as session:
        view = await event_service.detail(session, event_id)
        await event_service.sign_up(
            session, slot_id=view.slots[0].slot.id, volunteer_id=ids["mia"]
        )

    async with user_simulation(main_file=SIM_MAIN) as user:
        # Mia hands the slot straight to Noor
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open(f"/events/{event_id}")
        user.find("Hand off", kind=ui.button).click()
        await user.should_see("Hand this slot to a teammate")
        await user.should_see("goes into the log")
        pick = user.find(kind=ui.select, content="Who takes it?").elements.pop()
        pick.value = ids["noor"]
        user.find("Hand it over", kind=ui.button).click()
        await user.should_see("Noor Member now holds the slot", retries=30)
        assert [m[0] for m in sent_mail] == ["noor@example.org"]
        assert "You're now serving" in sent_mail[0][1]
        assert "Mia Member" in sent_mail[0][2]
        sent_mail.clear()

        # Noor takes themselves off; the reason goes to the leaders
        await user.open(f"/login-dev/{ids['noor_u']}")
        await user.open(f"/events/{event_id}")
        user.find("Withdraw", kind=ui.button).click()
        await user.should_see("your reason is emailed")
        box = user.find(kind=ui.textarea).elements.pop()
        box.value = "travelling that weekend"
        user.find("Take me off", kind=ui.button).click()
        await user.should_see("the leaders have been told", retries=30)
        assert {m[0] for m in sent_mail} == {"lena@example.org"}
        assert "Off the roster" in sent_mail[0][1]
        assert "travelling that weekend" in sent_mail[0][2]


async def test_duplicate_location_warning_on_create(database):
    async with db_session() as session:
        ids = await _parish(session)
    async with db_session() as session:
        await event_service.create_event(
            session,
            team_id=ids["liturgy"],
            title="Sunday Mass",
            starts_at=_next_week(10),
            ends_at=_next_week(12),
            location="Parish Hall",
            created_by=None,
        )

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open("/events")
        user.find("New event", kind=ui.button).click()
        await user.should_see("Repeat weekly until")
        team = user.find(kind=ui.select, content="Team").elements.pop()
        team.value = ids["liturgy"]
        title = user.find(kind=ui.input, content="Title").elements.pop()
        title.value = "Bake sale"
        day = user.find(kind=ui.input, content="Date (YYYY-MM-DD)").elements.pop()
        day.value = str(date.today() + timedelta(days=7))
        loc = user.find(kind=ui.input, content="Location").elements.pop()
        loc.value = "parish hall"
        user.find("Create event", kind=ui.button).click()
        await user.should_see("Possible double booking", retries=30)
        await user.should_see("Sunday Mass")
        user.find("Go back", kind=ui.button).click()

        # the form is still open; forcing it through this time works
        user.find("Create event", kind=ui.button).click()
        await user.should_see("Possible double booking", retries=30)
        user.find("Create anyway", kind=ui.button).click()
        await user.should_see("0/∞", retries=50)


async def test_attendance_section_on_past_event(database):
    async with db_session() as session:
        ids = await _parish(session)
    event_id = await _seed_event(ids["liturgy"])
    async with db_session() as session:
        view = await event_service.detail(session, event_id)
        await event_service.sign_up(
            session, slot_id=view.slots[0].slot.id, volunteer_id=ids["mia"]
        )
        past = datetime.combine(date.today() - timedelta(days=2), time(9), TZ)
        await event_service.update_event(
            session, event_id, starts_at=past, ends_at=past + timedelta(hours=2)
        )

    async with user_simulation(main_file=SIM_MAIN) as user:
        # members see the event but no attendance section
        await user.open(f"/login-dev/{ids['mia_u']}")
        await user.open(f"/events/{event_id}")
        await user.should_not_see("Attendance")

        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/events/{event_id}")
        await user.should_see("Attendance")
        await user.should_see("Mia Member")
        box = user.find(kind=ui.checkbox, content="attended").elements.pop()
        box.value = False
        user.find("Save", kind=ui.button).click()
        # the notification proves the save task finished before teardown
        await user.should_see("Attendance saved", retries=50)

    async with db_session() as session:
        summary = await event_service.hours_for_volunteer(session, ids["mia"])
        assert summary.events_attended == 0, "the no-show override took"


async def test_cancel_event_mails_assignees(database, sent_mail):
    async with db_session() as session:
        ids = await _parish(session)
    event_id = await _seed_event(ids["liturgy"])
    async with db_session() as session:
        view = await event_service.detail(session, event_id)
        await event_service.sign_up(
            session, slot_id=view.slots[0].slot.id, volunteer_id=ids["mia"]
        )

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/events/{event_id}")
        user.find("Cancel event", kind=ui.button).click()
        await user.should_see("Cancel this event?")
        user.find("Yes, cancel it", kind=ui.button).click()
        await user.should_see("Cancelled", retries=30)

    assert [m[0] for m in sent_mail] == ["mia@example.org"]
    assert "Cancelled: Sunday Mass" == sent_mail[0][1]
