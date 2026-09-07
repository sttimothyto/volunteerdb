"""Counts and reports are announced (uiux-improvement.md, step 36): the
count under every searchable table and the .csv import report are polite
live regions."""

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.models import TeamRole
from volunteerdb.permissions import SYSTEM
from volunteerdb.services import memberships, teams, users, volunteers

from tests import mint
from tests.conftest import SIM_MAIN, db_session, only
from tests.fp_helpers import ok


async def test_the_counts_and_the_import_report_are_live_regions(database):
    async with db_session() as session:
        music = ok(await teams.create(session, SYSTEM, "Music"))
        lena = ok(
            await volunteers.create(
                session, SYSTEM, "Lena", "Leader", "lena@example.org"
            )
        )
        ok(
            await memberships.assign(
                session, SYSTEM, lena.id, music.id, TeamRole.leader
            )
        )
        admin, _ = ok(
            await users.create(
                session,
                "admin@example.org",
                is_admin=True,
                invite=mint.fresh_invite(),
                actor=SYSTEM,
            )
        )
        ids = {"music": music.id, "admin_u": admin.id}

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['admin_u']}")
        for path, text in (
            ("/teams", "1 team"),
            ("/events", "0 events"),
            (f"/teams/{ids['music']}", "1 member"),
            ("/admin/users", "1 account"),
        ):
            await user.open(path)
            count = only(user.find(text, kind=ui.label))
            assert count.props.get("aria-live") == "polite", path

        await user.open(f"/teams/{ids['music']}")
        report = only(user.find(marker="import-report"))
        assert report.props.get("aria-live") == "polite"
