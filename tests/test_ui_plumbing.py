"""The team page folds its plumbing.

The roster spreadsheet, the .csv import and the volunteer home page are
panels under the roster: closed unless something is linked or the last sync
failed, each with a caption that says its state. The page reads chrome,
roster, events, then the plumbing."""

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from volunteerdb.models import TeamRole, TeamSheet
from volunteerdb.permissions import SYSTEM
from volunteerdb.services import memberships, pages, teams, users, volunteers

from tests import mint
from tests.conftest import SIM_MAIN, db_session, only
from tests.fp_helpers import ok


async def _parish(session) -> dict[str, int]:
    music = ok(await teams.create(session, SYSTEM, "Music"))
    lena = ok(
        await volunteers.create(session, SYSTEM, "Lena", "Leader", "lena@example.org")
    )
    ok(await memberships.assign(session, SYSTEM, lena.id, music.id, TeamRole.leader))
    lena_u, _ = ok(
        await users.create(
            session,
            "lena@example.org",
            volunteer_id=lena.id,
            invite=mint.fresh_invite(),
            actor=SYSTEM,
        )
    )
    return {"music": music.id, "lena_u": lena_u.id}


def _panel(user, marker: str) -> ui.expansion:
    return only(user.find(marker=marker))


async def test_the_panels_are_closed_until_something_is_linked(database):
    async with db_session() as session:
        ids = await _parish(session)

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/teams/{ids['music']}")
        sheet, imp, home = (
            _panel(user, "panel-sheet"),
            _panel(user, "panel-import"),
            _panel(user, "panel-home-page"),
        )
        assert not sheet.value and not imp.value and not home.value
        assert sheet.props["label"] == "Roster spreadsheet"
        assert sheet.props["caption"].startswith("Link a Google Sheet")
        assert home.props["caption"].startswith("Publish a Google Doc")
        assert imp.props["label"] == "Import a .csv"
        # the panels sit after the roster
        ids_in_order = [e.id for e in user.find(kind=ui.expansion).elements] + [
            only(user.find(marker="roster")).id
        ]
        assert min(ids_in_order[:-1]) > ids_in_order[-1]


async def test_a_linked_sheet_or_a_failed_sync_opens_the_panel(database):
    async with db_session() as session:
        ids = await _parish(session)
        session.add(
            TeamSheet(
                team_id=ids["music"],
                file_id="sheet-1",
                file_name="Music roster",
                last_status="error",
                last_error="not shared",
            )
        )
        ok(
            await pages.set_home_doc_url(
                session, SYSTEM, ids["music"], "https://docs.google.com/document/d/x1"
            )
        )

    async with user_simulation(main_file=SIM_MAIN) as user:
        await user.open(f"/login-dev/{ids['lena_u']}")
        await user.open(f"/teams/{ids['music']}")
        sheet, home = _panel(user, "panel-sheet"), _panel(user, "panel-home-page")
        assert sheet.value and sheet.props["caption"] == "The last sync failed"
        await user.should_see("Last sync failed: not shared")
        assert home.value and home.props["caption"] == "Doc linked · not published yet"
        assert not _panel(user, "panel-import").value
