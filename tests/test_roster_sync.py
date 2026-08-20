"""The roster sync service and its nightly job.

All Google traffic is stubbed at the services.gsheets boundary, the way
test_calendar_sync.py stubs gcal: what is under test is the orchestration —
which direction runs, what gets written back, what a failure does to the
team_sheet row, and the rule the whole feature rests on, that a sync never
removes anybody.
"""

import csv
from io import StringIO

import pytest

from volunteerdb.db import db_session
from volunteerdb.jobs import roster_sync
from volunteerdb.models import SyncStatus, TeamRole, TeamSheet
from volunteerdb.services import gsheets, memberships, teams, users, volunteers
from volunteerdb.services import roster_sheets as service
from volunteerdb.sheets.common import ROSTER_HEADERS


def _csv_bytes(rows: list[list]) -> bytes:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(ROSTER_HEADERS)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


class FakeSheets:
    """Records what the sync asked Google to do, and answers with canned CSV."""

    def __init__(self, content: bytes = b""):
        self.content = content
        self.written: list[tuple[str, bytes]] = []
        self.created: list[str] = []
        self.shared: list[str] = []
        self.decorated: list[str] = []
        self.read_error: Exception | None = None

    async def read_csv(self, file_id):
        if self.read_error is not None:
            raise self.read_error
        return self.content

    async def write_rows(self, token, file_id, content):
        self.written.append((file_id, content))
        self.content = content  # a real sheet would now read back this way

    async def mint_token(self):
        return "tok"

    async def create_sheet(self, token, title):
        self.created.append(title)
        return "new-file-id"

    async def share_anyone_writer(self, token, file_id):
        self.shared.append(file_id)

    async def decorate(self, token, file_id, team_values):
        self.decorated.append(file_id)
        return True


@pytest.fixture
def fake(monkeypatch):
    """Patch the client onto both modules that reach for it, and force
    enabled() true without inventing credentials."""
    stub = FakeSheets()
    for target in (service, roster_sync):
        monkeypatch.setattr(target, "gsheets", stub, raising=False)
    monkeypatch.setattr(stub, "enabled", lambda: True, raising=False)
    monkeypatch.setattr(stub, "GSheetsError", gsheets.GSheetsError, raising=False)
    return stub


@pytest.fixture
async def choir(database):
    """Choir with Lena (leader) and Mia (member), already linked to a sheet."""
    async with db_session() as session:
        team = await teams.create(session, None, "Choir")
        lena = await volunteers.create(
            session, None, "Lena", "Leader", "lena@example.org"
        )
        mia = await volunteers.create(session, None, "Mia", "Member", "mia@example.org")
        await memberships.assign(session, None, lena.id, team.id, TeamRole.leader)
        await memberships.assign(session, None, mia.id, team.id, TeamRole.member)
        lena_u, _ = await users.create(
            session, "lena@example.org", volunteer_id=lena.id
        )
        session.add(TeamSheet(team_id=team.id, file_id="f123"))
        return {
            "team": team.id,
            "lena": lena.id,
            "mia": mia.id,
            "lena_user": lena_u.id,
        }


async def _roster_names(team_id: int) -> set[str]:
    async with db_session() as session:
        roster = await teams.roster(session, None, team_id)
    return {v.first_name for _m, v in roster}


# --- direction ---------------------------------------------------------------


async def test_export_overwrites_the_sheet_from_the_database(choir, fake):
    outcome = await service.sync_team(
        choir["team"], direction=service.EXPORT, user_id=None
    )
    assert not outcome.failed
    assert len(fake.written) == 1
    written = fake.written[0][1].decode("utf-8-sig")
    assert "Lena" in written and "Mia" in written


async def test_import_applies_the_sheet_then_writes_it_back(choir, fake):
    fake.content = _csv_bytes(
        [
            ["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "leader"],
            ["", "Mia", "Member", "mia@example.org", "", "", "Choir", "member"],
            ["", "Nina", "New", "nina@example.org", "", "", "Choir", "member"],
        ]
    )
    outcome = await service.sync_team(
        choir["team"], direction=service.IMPORT, user_id=None
    )
    assert not outcome.failed
    assert outcome.report.volunteers_created == 1
    assert await _roster_names(choir["team"]) == {"Lena", "Mia", "Nina"}
    assert fake.written, "the sheet is rewritten so it shows what the DB now holds"


async def test_a_sync_never_removes_and_puts_the_missing_row_back(choir, fake):
    """The rule the feature rests on, end to end: Mia is deleted from the
    sheet, keeps her membership, and reappears in the written-back sheet."""
    fake.content = _csv_bytes(
        [["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "leader"]]
    )
    outcome = await service.sync_team(
        choir["team"], direction=service.IMPORT, user_id=None
    )
    assert not outcome.failed
    assert outcome.report.memberships_removed == 0
    assert await _roster_names(choir["team"]) == {"Lena", "Mia"}
    assert "Mia" in fake.written[0][1].decode("utf-8-sig")


# --- failure handling --------------------------------------------------------


async def test_an_unshared_sheet_records_the_error_and_writes_nothing(choir, fake):
    fake.read_error = gsheets.GSheetsError("the spreadsheet is not shared")
    outcome = await service.sync_team(
        choir["team"], direction=service.IMPORT, user_id=None
    )
    assert outcome.failed
    assert "not shared" in outcome.message
    assert fake.written == [], "a leader's sheet is never overwritten after a failure"
    async with db_session() as session:
        sheet = await session.get(TeamSheet, choir["team"])
    assert sheet.last_status == SyncStatus.error
    assert sheet.last_synced_at is None


async def test_a_bad_row_fails_the_team_whole_and_leaves_the_sheet_alone(choir, fake):
    fake.content = _csv_bytes(
        [["", "Lena", "Leader", "lena@example.org", "", "", "Choir", "archbishop"]]
    )
    outcome = await service.sync_team(
        choir["team"], direction=service.IMPORT, user_id=None
    )
    assert outcome.failed
    assert fake.written == []
    assert await _roster_names(choir["team"]) == {"Lena", "Mia"}


async def test_a_team_with_no_sheet_cannot_be_synced(database, fake):
    async with db_session() as session:
        team = await teams.create(session, None, "Choir")
    with pytest.raises(LookupError):
        await service.sync_team(team.id, direction=service.EXPORT, user_id=None)


async def test_an_unknown_direction_is_refused(choir, fake):
    with pytest.raises(ValueError, match="unknown sync direction"):
        await service.sync_team(choir["team"], direction="sideways", user_id=None)


# --- permissions -------------------------------------------------------------


async def test_a_plain_member_may_not_sync(choir, fake, database):
    async with db_session() as session:
        mia_u, _ = await users.create(
            session, "mia2@example.org", volunteer_id=choir["mia"]
        )
        mia_user_id = mia_u.id

    from volunteerdb.permissions import Forbidden

    with pytest.raises(Forbidden):
        await service.sync_team(
            choir["team"], direction=service.EXPORT, user_id=mia_user_id
        )


async def test_the_leader_may_sync(choir, fake):
    outcome = await service.sync_team(
        choir["team"], direction=service.EXPORT, user_id=choir["lena_user"]
    )
    assert not outcome.failed


# --- the job -----------------------------------------------------------------


async def test_the_job_creates_a_sheet_for_a_team_that_has_none(database, fake):
    async with db_session() as session:
        team = await teams.create(session, None, "Choir")
        lena = await volunteers.create(
            session, None, "Lena", "Leader", "lena@example.org"
        )
        await memberships.assign(session, None, lena.id, team.id, TeamRole.leader)
        team_id = team.id

    assert await roster_sync.main() == 0
    assert fake.created == ["choir-membership-list"]
    assert fake.shared == ["new-file-id"], "link sharing IS the leaders' access"
    async with db_session() as session:
        sheet = await session.get(TeamSheet, team_id)
    assert sheet.file_id == "new-file-id"


async def test_the_job_never_gives_a_task_force_a_sheet(database, fake, monkeypatch):
    """A meta team holds a borrowed roster; a link-shared sheet of it would
    publish the collaborating teams' contact details."""
    async with db_session() as session:
        team = await teams.create(session, None, "Choir")
        meta = await teams.create(session, None, "Task force")
        meta_id = meta.id
        team_id = team.id

    async def only_meta(session):
        return {meta_id}

    monkeypatch.setattr(roster_sync.team_service, "meta_team_ids", only_meta)
    assert await roster_sync.main() == 0
    async with db_session() as session:
        assert await session.get(TeamSheet, meta_id) is None
        assert (await session.get(TeamSheet, team_id)) is not None


async def test_the_job_is_a_noop_when_unconfigured(database, monkeypatch):
    monkeypatch.setattr(gsheets, "enabled", lambda: False)
    assert await roster_sync.main() == 0


async def test_one_broken_sheet_does_not_stop_the_others(database, fake):
    async with db_session() as session:
        good = await teams.create(session, None, "Choir")
        bad = await teams.create(session, None, "Altar")
        session.add(TeamSheet(team_id=good.id, file_id="good1"))
        session.add(TeamSheet(team_id=bad.id, file_id="bad1"))
        good_id, bad_id = good.id, bad.id

    real_read = fake.read_csv

    async def selective(file_id):
        if file_id == "bad1":
            raise gsheets.GSheetsError("the spreadsheet is not shared")
        return await real_read(file_id)

    fake.read_csv = selective
    fake.content = _csv_bytes([])

    assert await roster_sync.main() == 0
    async with db_session() as session:
        assert (await session.get(TeamSheet, bad_id)).last_status == SyncStatus.error
        assert (await session.get(TeamSheet, good_id)).last_status != SyncStatus.error
