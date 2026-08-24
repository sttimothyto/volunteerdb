"""The team file routes that replaced ui.download(): who may fetch what, the
filename each answers with, and that an anonymous browser is sent to sign
in rather than served."""

from volunteerdb.db import db_session
from volunteerdb.models import TeamRole
from volunteerdb.services import memberships, teams, users, volunteers


async def _seed() -> dict:
    async with db_session() as session:
        liturgy = await teams.create(session, None, "Liturgy Team")
        choir = await teams.create(session, None, "Choir")
        lena = await volunteers.create(
            session, None, "Lena", "Leader", "lena@example.org"
        )
        mia = await volunteers.create(session, None, "Mia", "Member", "mia@example.org")
        await memberships.assign(session, None, lena.id, liturgy.id, TeamRole.leader)
        await memberships.assign(session, None, mia.id, choir.id, TeamRole.member)
        admin, _ = await users.create(session, "admin@example.org", is_admin=True)
        lena_u, _ = await users.create(
            session, "lena@example.org", volunteer_id=lena.id
        )
        mia_u, _ = await users.create(session, "mia@example.org", volunteer_id=mia.id)
        return {
            "liturgy": liturgy.id,
            "choir": choir.id,
            "admin_u": admin.id,
            "lena_u": lena_u.id,
            "mia_u": mia_u.id,
        }


async def test_anonymous_browsers_are_sent_to_sign_in(real_app_client):
    ids = await _seed()
    for path in (
        "/export/teams.csv",
        "/export/roster-template.csv",
        f"/teams/{ids['liturgy']}/roster.csv",
        f"/teams/{ids['liturgy']}/qr.png",
    ):
        r = await real_app_client.get(path, follow_redirects=False)
        assert r.status_code in (302, 303, 307) and "/login" in r.headers["location"], (
            path
        )


async def test_the_parish_export_is_the_admins_and_my_teams_the_leaders(
    real_app_client,
):
    ids = await _seed()
    await real_app_client.get(f"/login-dev/{ids['admin_u']}")
    r = await real_app_client.get("/export/teams.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert (
        r.headers["content-disposition"]
        == 'attachment; filename="volunteerdb-parish.csv"'
    )
    assert "Lena" in r.text and "Mia" in r.text

    await real_app_client.get(f"/login-dev/{ids['lena_u']}")
    r = await real_app_client.get("/export/teams.csv")
    assert r.status_code == 200
    assert (
        r.headers["content-disposition"]
        == 'attachment; filename="volunteerdb-my-teams.csv"'
    )
    assert "Lena" in r.text and "Mia" not in r.text, "her team, not the choir"

    await real_app_client.get(f"/login-dev/{ids['mia_u']}")
    r = await real_app_client.get("/export/teams.csv")
    assert r.status_code == 403, "a plain member has no teams to export"


async def test_a_roster_needs_full_rights_on_that_team(real_app_client):
    ids = await _seed()
    await real_app_client.get(f"/login-dev/{ids['lena_u']}")
    r = await real_app_client.get(f"/teams/{ids['liturgy']}/roster.csv")
    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="liturgy-team.csv"'
    assert "Lena" in r.text
    r = await real_app_client.get(f"/teams/{ids['choir']}/roster.csv")
    assert r.status_code == 403
    r = await real_app_client.get("/teams/999999/roster.csv")
    assert r.status_code == 404
    r = await real_app_client.get(
        f"/teams/{ids['liturgy']}/roster.csv?as_of=2020-01-01"
    )
    assert r.status_code == 200 and "Lena" not in r.text, "a snapshot before she joined"


async def test_the_template_and_the_qr(real_app_client):
    ids = await _seed()
    await real_app_client.get(f"/login-dev/{ids['mia_u']}")
    r = await real_app_client.get("/export/roster-template.csv")
    assert r.status_code == 200 and "ID" in r.text
    assert (
        r.headers["content-disposition"]
        == 'attachment; filename="volunteerdb-template.csv"'
    )
    r = await real_app_client.get(f"/teams/{ids['liturgy']}/qr.png")
    assert r.status_code == 404, "no published page, no code for it"
