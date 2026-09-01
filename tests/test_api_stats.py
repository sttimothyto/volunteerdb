"""GET /api/reports/dashboard: the same tiering the page shows, over HTTP.

The endpoint takes no `require` — there is no single right to ask for — so
these tests are what pins the promise that each tier carries its own. A null
section means the service declined to compute it for this caller.
"""

from datetime import timedelta

from tests import mint


async def test_dashboard_admin_sees_parish_and_leadership(client, token_admin):
    r = await client.get("/api/reports/dashboard", headers=token_admin)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["live"] is True
    assert body["parish"]["active_volunteers"] == 1, "seeded Maria"
    assert body["parish"]["active_teams"] == 1
    assert body["parish"]["accounts"] == 1, "Maria's account, not the admin's"
    assert body["leadership"]["teams"] == 1
    assert body["leadership"]["teams_without_leader"] == 1, "seeded Liturgy has none"
    assert body["personal"] is None, "the admin account has no volunteer record"


async def test_dashboard_leader_sees_leadership_but_not_the_parish(
    client, token_leader
):
    r = await client.get("/api/reports/dashboard", headers=token_leader)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["parish"] is None, "parish figures are admins only"
    lead = body["leadership"]
    assert lead["teams"] == 1 and lead["people"] == 2, "Maria and Lena"
    assert lead["teams_without_leader"] == 0, "Lena leads it"
    assert lead["teams_without_second"] == 1
    assert lead["bands"] is not None, "a leader may see workload on their team"
    assert body["personal"] is not None, "Lena is a volunteer too"


async def test_dashboard_core_sees_reach_but_not_gaps_or_workload(client, token_core):
    r = await client.get("/api/reports/dashboard", headers=token_core)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["parish"] is None
    lead = body["leadership"]
    assert lead is not None, "core reads the full roster, so the tier renders"
    assert lead["people"] == 2, "Maria and Cora"
    assert lead["teams_without_leader"] is None, "coverage needs managing the team"
    assert lead["gap_teams"] == []
    assert lead["bands"] is None, "workload is never shown to core members"


async def test_dashboard_member_sees_only_their_own_service(client, token_member):
    r = await client.get("/api/reports/dashboard", headers=token_member)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["parish"] is None
    assert body["leadership"] is None, "a plain member runs none of those queries"
    assert body["personal"]["upcoming_duties"] == 0
    assert body["personal"]["ballots_waiting"] == 0


async def test_dashboard_as_of_omits_the_live_only_figures(client, token_admin):
    tomorrow = mint.today() + timedelta(days=1)

    r = await client.get(
        f"/api/reports/dashboard?as_of={tomorrow.isoformat()}", headers=token_admin
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["live"] is False
    assert body["parish"]["active_volunteers"] == 1, "versioned, so still answerable"
    assert body["parish"]["accounts"] is None, "app_user is not system-versioned"
    assert body["leadership"]["understaffed_events"] is None
    assert body["leadership"]["open_elections"] is None
    assert body["personal"] is None


async def test_dashboard_needs_a_token(client, seeded):
    r = await client.get("/api/reports/dashboard")

    assert r.status_code == 401
