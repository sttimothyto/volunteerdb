"""Who may open which door, as one table.

Anonymous callers are refused structurally (test_app_surface.py sweeps every
route for a 401). Above that, each endpoint decides for itself which signed-in
roles it turns away, and until now those decisions were tested one at a time
where somebody thought of it. This is the matrix: for every endpoint below,
the roles that must be told no (403), and the roles that must not be. The
allowed side asserts only that the door opened -- what happens inside is the
endpoint's own test's business -- so a 404 for a missing sub-resource or a
422 for an empty body still counts as "let in".

Roles are conftest's: admin; Lena, who leads the seeded Liturgy team; Cora,
a core member of it; and the plain member Maria."""

from datetime import timedelta

import httpx
import pytest

from volunteerdb.models import FieldType, TeamRole
from volunteerdb.services import (
    custom_fields,
    elections,
    events,
    memberships,
    pages,
    task_force,
    teams,
    users,
    volunteers,
)

from tests import mint
from tests.conftest import db_session
from tests.fakes import FakeHttp
from tests.fp_helpers import done, ok

ADMIN, LEADER, CORE, MEMBER = "admin", "leader", "core", "member"
ALL = (ADMIN, LEADER, CORE, MEMBER)


@pytest.fixture
async def world(
    api_app, seeded, token_admin, token_leader, token_core, token_member
) -> dict:
    """Ids for the placeholders in MATRIX, plus a token per role. The app's
    HTTP goes to a fake Google that answers every fetch, so the one route
    that fetches on the caller's behalf never reaches the network."""
    api_app.state.env = api_app.state.env.with_(
        http=FakeHttp(
            lambda r: httpx.Response(200, text="<html><body><p>doc</p></body></html>")
        )
    )
    team_id, maria_id = seeded["team_id"], seeded["volunteer_id"]
    async with db_session() as session:
        ok(
            await pages.set_home_doc_url(
                session, None, team_id, "https://docs.google.com/document/d/doc1"
            )
        )
        ushers = ok(await teams.create(session, None, "Ushers"))
        walter = ok(await volunteers.create(session, None, "Walter", "Willing"))
        ok(await memberships.assign(session, None, walter.id, team_id, TeamRole.member))
        membership = await memberships.find(session, walter.id, team_id)
        field = ok(
            await custom_fields.create_def(session, None, "Shirt", FieldType.text)
        )
        start = mint.now() + timedelta(days=7)
        (event,) = ok(
            await events.create_event(
                session,
                None,
                team_id=team_id,
                title="Mass",
                starts_at=start,
                ends_at=start + timedelta(hours=1),
                slots=[events.SlotInput("Lector", 3)],
                created_by=None,
                tz=mint.tz(),
                series_id=mint.uuid(),
            )
        )
        view = ok(await events.detail(session, None, event.id))
        slot_id = view.slots[0].slot.id
        # a second event, staffed by a task force with the Ushers, for the
        # task-force routes -- which look the task force up before deciding
        (tf_event,) = ok(
            await events.create_event(
                session,
                None,
                team_id=team_id,
                title="Picnic",
                starts_at=start,
                ends_at=start + timedelta(hours=3),
                created_by=None,
                tz=mint.tz(),
                series_id=mint.uuid(),
            )
        )
        done(
            await task_force.add_collaborating_team(
                session,
                None,
                event_id=tf_event.id,
                source_team_id=ushers.id,
                created_by=None,
                now=mint.now(),
                tz=mint.tz(),
            )
        )
        assignment = ok(
            await events.assign(
                session,
                None,
                slot_id=slot_id,
                volunteer_id=walter.id,
                assigned_by=None,
                now=mint.now(),
            )
        )
        admin = await users.get_by_email(session, "admin@example.org")
        proposal = ok(
            await elections.create_proposal(
                session,
                None,
                team_id=team_id,
                role=TeamRole.second,
                nomination_deadline=mint.today() + timedelta(days=3),
                voting_deadline=mint.today() + timedelta(days=10),
                created_by=admin.id,
                candidates=[elections.CandidateInput(walter.id)],
                today=mint.today(),
            )
        )
    return {
        "team_id": team_id,
        "membership_id": membership.id,
        "field_id": field.id,
        "event_id": event.id,
        "tf_event_id": tf_event.id,
        "slot_id": slot_id,
        "assignment_id": assignment.id,
        "proposal_id": proposal.id,
        "volunteer_id": maria_id,
        "tokens": {
            ADMIN: token_admin,
            LEADER: token_leader,
            CORE: token_core,
            MEMBER: token_member,
        },
    }


# (method, path, body, forbidden roles, allowed roles)
SIMILAR = (
    "/api/events/similar?starts_at=2030-01-01T10:00:00Z&ends_at=2030-01-01T11:00:00Z"
)
SHEET = "https://docs.google.com/spreadsheets/d/abc/edit"
WORKLOAD = {
    "multipliers": {"leader": 2, "second": 1.5, "core": 1.2, "member": 1},
    "bands": [{"label": "light", "color": "#cfe8cf", "upper": None}],
}
MATRIX = [
    (
        "PATCH",
        "/api/memberships/{membership_id}",
        {"role": "core"},
        (CORE, MEMBER),
        (ADMIN, LEADER),
    ),
    ("DELETE", "/api/custom-fields/{field_id}", None, (LEADER, CORE, MEMBER), (ADMIN,)),
    (
        "POST",
        "/api/teams/{team_id}/roster-sheet/sync",
        {"direction": "export"},
        (CORE, MEMBER),
        (ADMIN, LEADER),
    ),
    ("GET", "/api/teams/{team_id}/roster-sheet", None, (CORE, MEMBER), (ADMIN, LEADER)),
    (
        "PATCH",
        "/api/teams/{team_id}/roster-sheet",
        {"url": SHEET},
        (CORE, MEMBER),
        (ADMIN, LEADER),
    ),
    ("POST", "/api/teams/{team_id}/page/fetch", None, (MEMBER,), (ADMIN, LEADER, CORE)),
    ("GET", "/api/teams/{team_id}/page", None, (MEMBER,), (ADMIN, LEADER, CORE)),
    ("POST", "/api/events/{event_id}/cancel", None, (CORE, MEMBER), (ADMIN, LEADER)),
    ("GET", "/api/events/{tf_event_id}/task-force", None, (), ALL),
    (
        "POST",
        "/api/events/{event_id}/collaborators",
        {"team_id": 999999},
        (CORE, MEMBER),
        (ADMIN, LEADER),
    ),
    (
        "POST",
        "/api/events/{tf_event_id}/task-force/refresh",
        None,
        (CORE, MEMBER),
        (ADMIN, LEADER),
    ),
    (
        "POST",
        "/api/events/assignments/{assignment_id}/substitute",
        {"volunteer_id": 999999},
        (CORE, MEMBER),
        (ADMIN, LEADER),
    ),
    ("GET", "/api/events/mine", None, (ADMIN,), (LEADER, CORE, MEMBER)),
    ("GET", "/api/events/claimable", None, (ADMIN,), (LEADER, CORE, MEMBER)),
    ("GET", SIMILAR, None, (CORE, MEMBER), (ADMIN, LEADER)),
    ("GET", "/api/elections/vacancies", None, (MEMBER,), (ADMIN, LEADER, CORE)),
    (
        "POST",
        "/api/elections/proposals/{proposal_id}/candidates",
        {"volunteer_id": 999999},
        (MEMBER,),
        (ADMIN, LEADER, CORE),
    ),
    (
        "DELETE",
        "/api/elections/proposals/{proposal_id}/voters/999999",
        None,
        (CORE, MEMBER),
        (ADMIN, LEADER),
    ),
    # a non-voter reads an empty ballot of their own, not a refusal: nothing leaks
    (
        "GET",
        "/api/elections/proposals/{proposal_id}/ballot",
        None,
        (ADMIN,),
        (LEADER, CORE, MEMBER),
    ),
    (
        "PUT",
        "/api/events/{event_id}/rsvp",
        {"available": True},
        (ADMIN,),
        (LEADER, CORE, MEMBER),
    ),
    ("GET", "/api/volunteers/{volunteer_id}/assignments", None, (), ALL),
    ("POST", "/api/users/provision", None, (LEADER, CORE, MEMBER), (ADMIN,)),
    ("PUT", "/api/workload/config", WORKLOAD, (LEADER, CORE, MEMBER), (ADMIN,)),
]


def _cases():
    for method, path, body, forbidden, allowed in MATRIX:
        for role in ALL:
            if role in forbidden:
                yield pytest.param(
                    method, path, body, role, True, id=f"{method} {path} {role}:403"
                )
            elif role in allowed:
                yield pytest.param(
                    method, path, body, role, False, id=f"{method} {path} {role}:in"
                )


@pytest.mark.parametrize(("method", "path", "body", "role", "refused"), list(_cases()))
async def test_the_door(client, world, method, path, body, role, refused):
    url = path.format(**world)
    r = await client.request(method, url, json=body, headers=world["tokens"][role])
    if refused:
        assert r.status_code == 403, (role, r.status_code, r.text)
    else:
        assert r.status_code not in (401, 403), (role, r.status_code, r.text)
