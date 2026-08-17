"""The scheduling service: event creation (with weekly copy-forward and its
DST behaviour), slot capacity, RSVPs, the past-event roster freeze, open
substitution claims, and the derived attendance/hours record."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from volunteerdb.db import db_session
from volunteerdb.models import EventSubRequest, SubRequestStatus, TeamRole
from volunteerdb.permissions import load_actor
from volunteerdb.services import events as event_service
from volunteerdb.services import memberships, teams, users, volunteers

TZ = ZoneInfo("America/Toronto")


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, datetime.min.time(), TZ).replace(
        hour=hour, minute=minute
    )


async def _team_with_members(n: int = 2) -> tuple[int, list[int]]:
    """A team with volunteer 0 as leader and the rest as members."""
    async with db_session() as session:
        team = await teams.create(session, "Altar Servers")
        vids = []
        for i in range(n):
            v = await volunteers.create(
                session, f"Vol{i}", "Server", f"vol{i}@example.org"
            )
            role = TeamRole.leader if i == 0 else TeamRole.member
            await memberships.assign(session, v.id, team.id, role)
            vids.append(v.id)
        return team.id, vids


async def _one_event(
    team_id: int, *, start: datetime | None = None, hours: int = 2, **kwargs
) -> int:
    start = start or _at(date.today() + timedelta(days=7), 10)
    async with db_session() as session:
        created = await event_service.create_event(
            session,
            team_id=team_id,
            title="Sunday Mass",
            starts_at=start,
            ends_at=start + timedelta(hours=hours),
            created_by=None,
            **kwargs,
        )
        return created[0].id


async def _first_slot(event_id: int) -> int:
    async with db_session() as session:
        d = await event_service.detail(session, event_id)
        return d.slots[0].slot.id


async def _past_event(team_id: int, vid: int | None = None) -> tuple[int, int | None]:
    """A finished two-hour event; optionally with vid signed up (backdating
    via update_event, since the roster freezes once ends_at passes)."""
    start = _at(date.today() + timedelta(days=3), 9)
    event_id = await _one_event(team_id, start=start)
    assignment_id = None
    if vid is not None:
        async with db_session() as session:
            a = await event_service.sign_up(
                session, slot_id=await _first_slot(event_id), volunteer_id=vid
            )
            assignment_id = a.id
    past = _at(date.today() - timedelta(days=3), 9)
    async with db_session() as session:
        await event_service.update_event(
            session, event_id, starts_at=past, ends_at=past + timedelta(hours=2)
        )
    return event_id, assignment_id


# --- creation and copy-forward ---------------------------------------------


async def test_create_defaults_to_one_unlimited_volunteers_slot(database):
    team_id, _ = await _team_with_members()
    event_id = await _one_event(team_id)
    async with db_session() as session:
        d = await event_service.detail(session, event_id)
        assert [s.slot.name for s in d.slots] == ["Volunteers"]
        assert d.slots[0].slot.capacity is None
        assert d.slots[0].open_spots is None


async def test_create_with_explicit_slots_and_validation(database):
    team_id, _ = await _team_with_members()
    slots = [
        event_service.SlotInput("Lector", 2),
        event_service.SlotInput("Greeter", None, position=1),
    ]
    event_id = await _one_event(team_id, slots=slots)
    async with db_session() as session:
        d = await event_service.detail(session, event_id)
        assert [(s.slot.name, s.slot.capacity) for s in d.slots] == [
            ("Lector", 2),
            ("Greeter", None),
        ]
        for bad in (
            [event_service.SlotInput("")],
            [event_service.SlotInput("A"), event_service.SlotInput("A")],
            [event_service.SlotInput("A", 0)],
        ):
            with pytest.raises(ValueError):
                await event_service.create_event(
                    session,
                    team_id=team_id,
                    title="X",
                    starts_at=_at(date.today() + timedelta(days=1), 10),
                    ends_at=_at(date.today() + timedelta(days=1), 11),
                    slots=bad,
                    created_by=None,
                )
        with pytest.raises(ValueError):  # end before start
            await event_service.create_event(
                session,
                team_id=team_id,
                title="X",
                starts_at=_at(date.today() + timedelta(days=1), 11),
                ends_at=_at(date.today() + timedelta(days=1), 10),
                created_by=None,
            )


async def test_repeat_weekly_is_inclusive_and_copies_slots(database):
    team_id, _ = await _team_with_members()
    start = _at(date.today() + timedelta(days=7), 10)
    async with db_session() as session:
        created = await event_service.create_event(
            session,
            team_id=team_id,
            title="Sunday Mass",
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            slots=[event_service.SlotInput("Lector", 2)],
            repeat_weekly_until=start.date() + timedelta(days=14),
            created_by=None,
        )
        assert len(created) == 3, "day 0, 7 and 14 — until is inclusive"
        for e in created:
            d = await event_service.detail(session, e.id)
            assert [(s.slot.name, s.slot.capacity) for s in d.slots] == [("Lector", 2)]


async def test_repeat_keeps_wall_clock_time_across_dst(database):
    """A 10:30 Mass repeated over the November DST change stays 10:30 in the
    parish while its UTC offset shifts — never a blind timedelta(weeks=1)."""
    team_id, _ = await _team_with_members()
    start = _at(date(2026, 10, 25), 10, 30)  # EDT, a week before fall-back
    async with db_session() as session:
        created = await event_service.create_event(
            session,
            team_id=team_id,
            title="Sunday Mass",
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            repeat_weekly_until=date(2026, 11, 8),
            created_by=None,
        )
        locals_ = [e.starts_at.astimezone(TZ) for e in created]
        assert [dt.date() for dt in locals_] == [
            date(2026, 10, 25),
            date(2026, 11, 1),
            date(2026, 11, 8),
        ]
        assert all(dt.hour == 10 and dt.minute == 30 for dt in locals_)
        offsets = {dt.utcoffset() for dt in locals_}
        assert offsets == {timedelta(hours=-4), timedelta(hours=-5)}, (
            "the instant moved with the wall clock"
        )


async def test_repeat_is_capped_at_a_year(database):
    team_id, _ = await _team_with_members()
    start = _at(date.today() + timedelta(days=1), 10)
    async with db_session() as session:
        with pytest.raises(ValueError, match="one year"):
            await event_service.create_event(
                session,
                team_id=team_id,
                title="Forever Mass",
                starts_at=start,
                ends_at=start + timedelta(hours=1),
                repeat_weekly_until=start.date() + timedelta(days=400),
                created_by=None,
            )


# --- sign-up, capacity, RSVP ------------------------------------------------


async def test_capacity_fills_and_unlimited_never_does(database):
    team_id, vids = await _team_with_members(3)
    event_id = await _one_event(team_id, slots=[event_service.SlotInput("Lector", 1)])
    slot_id = await _first_slot(event_id)
    async with db_session() as session:
        await event_service.sign_up(session, slot_id=slot_id, volunteer_id=vids[1])
        with pytest.raises(ValueError, match="full"):
            await event_service.sign_up(session, slot_id=slot_id, volunteer_id=vids[2])

    open_id = await _one_event(team_id)  # default unlimited slot
    open_slot = await _first_slot(open_id)
    async with db_session() as session:
        for vid in vids:
            await event_service.sign_up(session, slot_id=open_slot, volunteer_id=vid)


async def test_one_slot_per_person_per_event(database):
    team_id, vids = await _team_with_members()
    event_id = await _one_event(
        team_id,
        slots=[
            event_service.SlotInput("Lector", 2),
            event_service.SlotInput("Greeter"),
        ],
    )
    async with db_session() as session:
        d = await event_service.detail(session, event_id)
        lector, greeter = (s.slot.id for s in d.slots)
        await event_service.sign_up(session, slot_id=lector, volunteer_id=vids[1])
        with pytest.raises(ValueError, match="already serve"):
            await event_service.sign_up(session, slot_id=greeter, volunteer_id=vids[1])


async def test_participation_requires_membership(database):
    team_id, _ = await _team_with_members(1)
    async with db_session() as session:
        outsider = await volunteers.create(session, "Out", "Sider", "out@example.org")
    event_id = await _one_event(team_id)
    slot_id = await _first_slot(event_id)
    async with db_session() as session:
        with pytest.raises(ValueError, match="members"):
            await event_service.sign_up(
                session, slot_id=slot_id, volunteer_id=outsider.id
            )
        with pytest.raises(ValueError, match="members"):
            await event_service.set_rsvp(
                session, event_id=event_id, volunteer_id=outsider.id, available=True
            )


async def test_rsvp_upserts_and_flips(database):
    team_id, vids = await _team_with_members()
    event_id = await _one_event(team_id)
    async with db_session() as session:
        await event_service.set_rsvp(
            session, event_id=event_id, volunteer_id=vids[1], available=True
        )
        await event_service.set_rsvp(
            session,
            event_id=event_id,
            volunteer_id=vids[1],
            available=False,
            note="away that week",
        )
        d = await event_service.detail(session, event_id)
        assert len(d.rsvps) == 1, "upsert, not a second row"
        rsvp, volunteer = d.rsvps[0]
        assert volunteer.id == vids[1]
        assert rsvp.available is False and rsvp.note == "away that week"


# --- the past-event freeze --------------------------------------------------


async def test_roster_mutations_freeze_after_the_event(database):
    team_id, vids = await _team_with_members(3)
    event_id, assignment_id = await _past_event(team_id, vids[1])
    slot_id = await _first_slot(event_id)
    async with db_session() as session:
        with pytest.raises(ValueError, match="ended"):
            await event_service.sign_up(session, slot_id=slot_id, volunteer_id=vids[2])
        with pytest.raises(ValueError, match="ended"):
            await event_service.assign(
                session, slot_id=slot_id, volunteer_id=vids[2], assigned_by=None
            )
        with pytest.raises(ValueError, match="ended"):
            await event_service.remove_assignment(session, assignment_id)
        with pytest.raises(ValueError, match="ended"):
            await event_service.request_sub(
                session, assignment_id=assignment_id, requested_by=None
            )
        with pytest.raises(ValueError, match="ended"):
            await event_service.set_rsvp(
                session, event_id=event_id, volunteer_id=vids[2], available=True
            )


# --- cancellation -----------------------------------------------------------


async def test_cancel_resolves_open_subs_and_returns_assignee_emails(database):
    team_id, vids = await _team_with_members(3)
    event_id = await _one_event(team_id)
    slot_id = await _first_slot(event_id)
    async with db_session() as session:
        a = await event_service.sign_up(session, slot_id=slot_id, volunteer_id=vids[1])
        sub = await event_service.request_sub(
            session, assignment_id=a.id, requested_by=None
        )
        sub_id = sub.id
    async with db_session() as session:
        event, emails = await event_service.cancel_event(
            session, event_id, cancelled_by=None
        )
        assert event.status == "cancelled" and event.cancelled_at is not None
        assert emails == ["vol1@example.org"]
        resolved = await session.get(EventSubRequest, sub_id)
        assert resolved.status == SubRequestStatus.cancelled.value
        assert resolved.resolved_at is not None
        with pytest.raises(ValueError, match="already cancelled"):
            await event_service.cancel_event(session, event_id, cancelled_by=None)
        with pytest.raises(ValueError, match="cancelled"):
            await event_service.sign_up(session, slot_id=slot_id, volunteer_id=vids[2])


# --- substitutions ----------------------------------------------------------


async def test_claim_moves_the_assignment_and_records_who(database):
    team_id, vids = await _team_with_members(3)
    event_id = await _one_event(team_id)
    slot_id = await _first_slot(event_id)
    async with db_session() as session:
        a = await event_service.sign_up(session, slot_id=slot_id, volunteer_id=vids[1])
        sub = await event_service.request_sub(
            session, assignment_id=a.id, requested_by=None, note="out of town"
        )
        with pytest.raises(ValueError, match="already open"):
            await event_service.request_sub(
                session, assignment_id=a.id, requested_by=None
            )
    async with db_session() as session:
        claimed, assignment, asker = await event_service.claim_sub(
            session, sub_request_id=sub.id, volunteer_id=vids[2]
        )
        assert asker.id == vids[1], "the caller mails the person who asked"
        assert claimed.status == SubRequestStatus.claimed.value
        assert claimed.claimed_by_volunteer_id == vids[2]
        assert assignment.volunteer_id == vids[2]
        assert assignment.kind == "sub"
        assert assignment.assigned_notified_at is not None, "claimant acted themselves"
        assert assignment.reminder_sent_at is None, "new person still gets a reminder"
    async with db_session() as session:
        with pytest.raises(ValueError, match="already claimed"):
            await event_service.claim_sub(
                session, sub_request_id=sub.id, volunteer_id=vids[0]
            )


async def test_claim_rejects_own_slot_and_double_booking(database):
    team_id, vids = await _team_with_members(3)
    event_id = await _one_event(
        team_id,
        slots=[
            event_service.SlotInput("Lector", 2),
            event_service.SlotInput("Greeter"),
        ],
    )
    async with db_session() as session:
        d = await event_service.detail(session, event_id)
        lector, greeter = (s.slot.id for s in d.slots)
        a1 = await event_service.sign_up(session, slot_id=lector, volunteer_id=vids[1])
        await event_service.sign_up(session, slot_id=greeter, volunteer_id=vids[2])
        sub = await event_service.request_sub(
            session, assignment_id=a1.id, requested_by=None
        )
        with pytest.raises(ValueError, match="your own"):
            await event_service.claim_sub(
                session, sub_request_id=sub.id, volunteer_id=vids[1]
            )
        with pytest.raises(ValueError, match="already serve"):
            await event_service.claim_sub(
                session, sub_request_id=sub.id, volunteer_id=vids[2]
            )


async def test_cancel_sub_and_claimable_visibility(database):
    team_id, vids = await _team_with_members(3)
    event_id = await _one_event(team_id)
    slot_id = await _first_slot(event_id)
    async with db_session() as session:
        a = await event_service.sign_up(session, slot_id=slot_id, volunteer_id=vids[1])
        sub = await event_service.request_sub(
            session, assignment_id=a.id, requested_by=None
        )
        user2 = await users.create(session, "vol2@example.org", volunteer_id=vids[2])
        actor2 = await load_actor(session, user2)
        claimable = await event_service.claimable_subs(session, actor2)
        assert [c.sub.id for c in claimable] == [sub.id]
        assert claimable[0].volunteer.id == vids[1]

        user1 = await users.create(session, "vol1@example.org", volunteer_id=vids[1])
        actor1 = await load_actor(session, user1)
        assert await event_service.claimable_subs(session, actor1) == [], (
            "your own request is not claimable by you"
        )

        await event_service.cancel_sub(session, sub.id)
        assert await event_service.claimable_subs(session, actor2) == []
        with pytest.raises(ValueError, match="already cancelled"):
            await event_service.cancel_sub(session, sub.id)
        duties = await event_service.my_upcoming(session, vids[1])
        assert len(duties) == 1 and duties[0].open_sub is None


# --- attendance and hours ---------------------------------------------------


async def test_attendance_derives_and_overrides(database):
    team_id, vids = await _team_with_members()
    event_id, assignment_id = await _past_event(team_id, vids[1])
    async with db_session() as session:
        event = await event_service.get(session, event_id)
        assignment = await event_service.get_assignment(session, assignment_id)
        assert event_service.effective(assignment, event) == (True, Decimal("2.00"))

        await event_service.set_attendance(
            session, assignment_id=assignment_id, attended=False, hours=None
        )
        assert event_service.effective(assignment, event) == (False, Decimal("0.00"))

        await event_service.set_attendance(
            session, assignment_id=assignment_id, attended=True, hours=Decimal("3.5")
        )
        assert event_service.effective(assignment, event) == (True, Decimal("3.5"))

        await event_service.set_attendance(
            session, assignment_id=assignment_id, attended=None, hours=None
        )
        assert event_service.effective(assignment, event) == (True, Decimal("2.00")), (
            "None clears the override back to auto"
        )
        with pytest.raises(ValueError, match="negative"):
            await event_service.set_attendance(
                session,
                assignment_id=assignment_id,
                attended=True,
                hours=Decimal("-1"),
            )


async def test_attendance_needs_a_finished_uncancelled_event(database):
    team_id, vids = await _team_with_members()
    future_id = await _one_event(team_id)
    slot_id = await _first_slot(future_id)
    async with db_session() as session:
        a = await event_service.sign_up(session, slot_id=slot_id, volunteer_id=vids[1])
        with pytest.raises(ValueError, match="after the event ends"):
            await event_service.set_attendance(
                session, assignment_id=a.id, attended=False, hours=None
            )


async def test_hours_sum_past_uncancelled_events_only(database):
    team_id, vids = await _team_with_members()
    await _past_event(team_id, vids[1])  # 2h auto
    event2, a2 = await _past_event(team_id, vids[1])  # overridden to 1.25h
    cancelled, _ = await _past_event(team_id, vids[1])  # cancelled: excluded
    future = await _one_event(team_id)  # upcoming: excluded
    async with db_session() as session:
        await event_service.set_attendance(
            session, assignment_id=a2, attended=True, hours=Decimal("1.25")
        )
        await event_service.sign_up(
            session, slot_id=await _first_slot(future), volunteer_id=vids[1]
        )
    async with db_session() as session:
        await event_service.cancel_event(session, cancelled, cancelled_by=None)
    async with db_session() as session:
        summary = await event_service.hours_for_volunteer(session, vids[1])
        assert summary.events_attended == 2
        assert summary.total_hours == Decimal("3.25")


# --- listings and scoping ---------------------------------------------------


async def test_list_events_scopes_to_the_actors_teams(database):
    team_a, vids_a = await _team_with_members(2)
    async with db_session() as session:
        team_b = await teams.create(session, "Choir")
        other = await volunteers.create(session, "Oda", "Choir", "oda@example.org")
        await memberships.assign(session, other.id, team_b.id, TeamRole.member)
        admin = await users.create(session, "admin@example.org", is_admin=True)
    await _one_event(team_a)
    b_start = _at(date.today() + timedelta(days=7), 18)
    async with db_session() as session:
        await event_service.create_event(
            session,
            team_id=team_b.id,
            title="Choir practice",
            starts_at=b_start,
            ends_at=b_start + timedelta(hours=2),
            created_by=None,
        )
    async with db_session() as session:
        member = await users.create(session, "vol1@example.org", volunteer_id=vids_a[1])
        member_actor = await load_actor(session, member)
        admin_actor = await load_actor(session, admin)
        mine = await event_service.list_events(session, member_actor)
        assert [s.event.title for s in mine] == ["Sunday Mass"]
        assert mine[0].path == "Altar Servers"
        everything = await event_service.list_events(session, admin_actor)
        assert {s.event.title for s in everything} == {"Sunday Mass", "Choir practice"}


async def test_summary_counts_fill_and_capacity(database):
    team_id, vids = await _team_with_members(3)
    event_id = await _one_event(
        team_id,
        slots=[
            event_service.SlotInput("Lector", 2),
            event_service.SlotInput("Usher", 3),
        ],
    )
    async with db_session() as session:
        await event_service.sign_up(
            session, slot_id=await _first_slot(event_id), volunteer_id=vids[1]
        )
        leader = await users.create(session, "vol0@example.org", volunteer_id=vids[0])
        actor = await load_actor(session, leader)
        summary = (await event_service.list_events(session, actor))[0]
        assert (summary.filled, summary.capacity) == (1, 5)
        assert summary.my_assignment is None
        unlimited = await _one_event(team_id)
        assert unlimited  # any unlimited slot makes the event unlimited
        summaries = await event_service.list_events(session, actor)
        assert summaries[-1].capacity is None


# --- the double-booking warning -----------------------------------------------


async def _admin_actor(session):
    admin = await users.create(session, "checker@example.org", is_admin=True)
    return await load_actor(session, admin)


async def test_similar_events_matches_fuzzy_same_day_locations(database):
    team_id, _ = await _team_with_members()
    day = date.today() + timedelta(days=7)
    await _one_event(team_id, location="Parish Hall")
    async with db_session() as session:
        actor = await _admin_actor(session)

        hits = await event_service.similar_events(
            session,
            actor,
            starts_at=_at(day, 14),
            ends_at=_at(day, 16),
            location="parish  hall (main)",
        )
        assert [h.title for h in hits] == ["Sunday Mass"], (
            "case, spacing, and a suffix still read as the same place"
        )

        next_day = day + timedelta(days=1)
        assert not await event_service.similar_events(
            session,
            actor,
            starts_at=_at(next_day, 14),
            ends_at=_at(next_day, 16),
            location="Parish Hall",
        ), "a different day is no collision"

        assert not await event_service.similar_events(
            session,
            actor,
            starts_at=_at(day, 14),
            ends_at=_at(day, 16),
            location="Rectory",
        ), "dissimilar locations stay quiet"

        assert not await event_service.similar_events(
            session,
            actor,
            starts_at=_at(day, 14),
            ends_at=_at(day, 16),
            location="",
        ), "no location, no check"


async def test_similar_events_masks_titles_outside_the_actors_scope(database):
    team_id, vids = await _team_with_members()
    day = date.today() + timedelta(days=7)
    async with db_session() as session:
        other = await teams.create(session, "Garden Guild")
        await event_service.create_event(
            session,
            team_id=other.id,
            title="Secret planning",
            starts_at=_at(day, 10),
            ends_at=_at(day, 12),
            location="Parish Hall",
            created_by=None,
        )
    async with db_session() as session:
        member = await users.create(session, "vol1@example.org", volunteer_id=vids[1])
        actor = await load_actor(session, member)
        hits = await event_service.similar_events(
            session,
            actor,
            starts_at=_at(day, 14),
            ends_at=_at(day, 16),
            location="Parish Hall",
        )
        assert [(h.title, h.team_path) for h in hits] == [(None, "Garden Guild")], (
            "the when/where warns; the invisible team's title stays masked"
        )


async def test_similar_events_checks_every_repeat_occurrence(database):
    team_id, _ = await _team_with_members()
    clash_day = date.today() + timedelta(days=21)
    await _one_event(team_id, start=_at(clash_day, 10), location="Parish Hall")
    first = date.today() + timedelta(days=7)
    async with db_session() as session:
        actor = await _admin_actor(session)
        hits = await event_service.similar_events(
            session,
            actor,
            starts_at=_at(first, 10),
            ends_at=_at(first, 12),
            repeat_until=first + timedelta(days=28),
            location="Parish Hall",
        )
        assert len(hits) == 1, "the collision sits three weeks into the repeat"
