"""The Google Sheets client (services/gsheets.py).

Tested here: the constants that must track the roster format (role labels,
column positions), the pure decoration builder and its safety property (field
masks that cannot reach cell values), state distillation, and the read/write
legs against a mocked transport — including the case that matters most in
practice, a sheet nobody remembered to share.

NOT tested: the live token mint and the Sheets/Drive APIs themselves. Those
belong to the runbook in docs/how-to/roster-spreadsheets.md, and this suite
follows test_calendar_sync.py in stubbing at the module boundary instead.

Most of this file is inherited from the retired test_decorate_sheets.py: the
decoration moved from a host-side script into the app when the rclone leg was
dropped, but the sheet it has to produce did not change.
"""

import httpx
import pytest

from volunteerdb.errors import External
from volunteerdb.models import ROLE_LABELS, TeamRole
from volunteerdb.services import gsheets
from volunteerdb.sheets.common import ROSTER_HEADERS

from tests.fp_helpers import ok, refused

pytestmark = pytest.mark.pure

# --- constants must track the roster format ----------------------------------


def test_role_labels_match_the_app_enum():
    assert gsheets.ROLE_VALUES == [ROLE_LABELS[role] for role in TeamRole], (
        "the dropdown must byte-match what the exporter writes, in enum order"
    )


def test_columns_match_the_roster_header():
    assert gsheets.ROLE_COLUMN == ROSTER_HEADERS.index("Role")
    assert gsheets.TEAM_COLUMN == ROSTER_HEADERS.index("Team")
    assert gsheets.ID_COLUMN == ROSTER_HEADERS.index("ID")


# --- state distillation and the compliance check -----------------------------


def _state(**overrides) -> dict:
    state = {
        "sheet_id": 5,
        "row_count": 40,
        "frozen": True,
        "id_hidden": True,
        "notes_ok": True,
        "dropdown_ok": True,
        "team_values": None,
        "marker_protections": [9],
    }
    state.update(overrides)
    return state


def _one_of_list(values) -> dict:
    return {
        "dataValidation": {
            "condition": {
                "type": "ONE_OF_LIST",
                "values": [{"userEnteredValue": v} for v in values],
            },
            "strict": True,
            "showCustomUi": True,
        }
    }


def _spreadsheet(
    note=None, labels=None, protections=None, row_count=40, teams=None
) -> dict:
    """A canned spreadsheets.get response in the shape GET_FIELDS asks for."""
    note = gsheets.WARNING_NOTE if note is None else note
    labels = gsheets.ROLE_VALUES if labels is None else labels
    if protections is None:
        protections = [
            {
                "protectedRangeId": 9,
                "description": gsheets.PROTECTION_MARKER,
                "warningOnly": True,
            }
        ]
    return {
        "sheets": [
            {
                "properties": {
                    "sheetId": 5,
                    "gridProperties": {"rowCount": row_count, "frozenRowCount": 1},
                },
                "protectedRanges": protections,
                "data": [
                    {
                        "rowData": [
                            {"values": [{"note": note}, {"note": note}]},
                            {
                                "values": [{}] * 6
                                + [
                                    {} if teams is None else _one_of_list(teams),
                                    _one_of_list(labels),
                                ]
                            },
                        ],
                        "columnMetadata": [{"hiddenByUser": True}] + [{}] * 7,
                    }
                ],
            }
        ]
    }


def test_compliant_sheet_is_skipped():
    state = gsheets.first_sheet_state(_spreadsheet())
    assert state == _state()
    assert gsheets.is_decorated(state)


def test_any_missing_piece_fails_the_compliance_check():
    for broken in (
        {"row_count": 1},
        {"frozen": False},
        {"id_hidden": False},
        {"notes_ok": False},
        {"dropdown_ok": False},
        {"marker_protections": []},
        {"marker_protections": [9, 10]},
    ):
        assert not gsheets.is_decorated(_state(**broken)), broken


def test_a_lenient_dropdown_is_not_a_dropdown():
    """strict=False lets a leader type anything past the list — the whole
    point of the rule is that the sync never sees an unknown value."""
    sheet = _spreadsheet()
    row = sheet["sheets"][0]["data"][0]["rowData"][1]["values"]
    row[gsheets.ROLE_COLUMN]["dataValidation"]["strict"] = False
    assert not gsheets.first_sheet_state(sheet)["dropdown_ok"]


def test_a_renamed_team_forces_redecoration():
    """The Team dropdown carries the team's display path, so a rename makes
    every existing cell an unknown value."""
    state = gsheets.first_sheet_state(_spreadsheet(teams=["Choir / Choir (Sun 8am)"]))
    assert gsheets.is_decorated(state, ["Choir / Choir (Sun 8am)"])
    assert not gsheets.is_decorated(state, ["Choir / Choir (Sun 08:00)"])
    assert gsheets.is_decorated(state, None), "None means leave the column alone"


def test_freshly_created_sheet_reads_as_undecorated():
    bare = {
        "sheets": [
            {
                "properties": {"sheetId": 0, "gridProperties": {"rowCount": 14}},
                "data": [{"rowData": [{"values": [{}]}], "columnMetadata": [{}]}],
            }
        ]
    }
    state = gsheets.first_sheet_state(bare)
    assert not gsheets.is_decorated(state)
    assert state["marker_protections"] == []


# --- the request builder -----------------------------------------------------


def test_build_requests_golden():
    requests = gsheets.build_requests(_state(marker_protections=[]), ["Choir"])
    kinds = [next(iter(r)) for r in requests]
    assert kinds == [
        "setDataValidation",  # Role
        "setDataValidation",  # Team
        "updateCells",
        "updateSheetProperties",
        "updateDimensionProperties",
        "addProtectedRange",
    ]
    validation = requests[0]["setDataValidation"]
    assert validation["range"] == {
        "sheetId": 5,
        "startRowIndex": 1,  # open-ended: no endRowIndex, all data rows
        "startColumnIndex": 7,
        "endColumnIndex": 8,
    }
    assert [
        v["userEnteredValue"] for v in validation["rule"]["condition"]["values"]
    ] == gsheets.ROLE_VALUES
    assert validation["rule"]["strict"] is True
    team = requests[1]["setDataValidation"]
    assert team["range"]["startColumnIndex"] == 6
    assert team["range"]["endColumnIndex"] == 7
    assert [v["userEnteredValue"] for v in team["rule"]["condition"]["values"]] == [
        "Choir"
    ]
    notes = requests[2]["updateCells"]
    assert notes["rows"] == [
        {
            "values": [
                {"note": gsheets.WARNING_NOTE},
                {"note": gsheets.WARNING_NOTE},  # B1 too: A1 hides with column A
            ]
        }
    ]
    protection = requests[5]["addProtectedRange"]["protectedRange"]
    assert protection["warningOnly"] is True
    assert protection["description"] == gsheets.PROTECTION_MARKER


def test_no_team_values_means_no_team_request():
    requests = gsheets.build_requests(_state(marker_protections=[]))
    validations = [r for r in requests if "setDataValidation" in r]
    assert len(validations) == 1
    assert validations[0]["setDataValidation"]["range"]["startColumnIndex"] == 7


def test_field_masks_cannot_reach_cell_values():
    """The safety property: every masked request touches metadata only."""
    requests = gsheets.build_requests(_state(marker_protections=[]), ["Choir"])
    masks = {
        next(iter(r)): r[next(iter(r))]["fields"]
        for r in requests
        if "fields" in r[next(iter(r))]
    }
    assert masks == {
        "updateCells": "note",
        "updateSheetProperties": "gridProperties.frozenRowCount",
        "updateDimensionProperties": "hiddenByUser",
    }


def test_header_only_sheet_gets_rows_appended_first():
    requests = gsheets.build_requests(_state(row_count=1))
    assert next(iter(requests[0])) == "appendDimension", (
        "must come first: setDataValidation on row 2 of a 1-row grid errors"
    )
    assert requests[0]["appendDimension"]["length"] == gsheets.BLANK_ROWS


def test_duplicate_marker_protections_are_pruned_not_readded():
    requests = gsheets.build_requests(_state(marker_protections=[3, 8, 11]))
    kinds = [next(iter(r)) for r in requests]
    assert "addProtectedRange" not in kinds
    deletes = [
        r["deleteProtectedRange"]["protectedRangeId"]
        for r in requests
        if "deleteProtectedRange" in r
    ]
    assert deletes == [8, 11], "the first survives; crash debris goes"


def test_an_oversized_team_list_skips_the_rule_rather_than_failing():
    too_many = [f"Team {n}" for n in range(gsheets.MAX_VALIDATION_VALUES + 1)]
    requests = gsheets.build_requests(_state(marker_protections=[]), too_many)
    validations = [r for r in requests if "setDataValidation" in r]
    assert len(validations) == 1, "Role survives; Team is dropped, not fatal"


# --- the read leg ------------------------------------------------------------


def _client(handler) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    """A client routed through a MockTransport, recording the requests (the
    idiom test_fetch_pages.py uses) — what env.HttpClients hands the service,
    so nothing is patched."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(record)), seen


async def test_read_csv_is_anonymous():
    """No Authorization header: link sharing is the access, and requiring a
    token would put us back where the drive.file grant left us."""
    client, seen = _client(lambda r: httpx.Response(200, content=b"ID,First name\n"))
    assert ok(await gsheets.read_csv(client, "abc123")) == b"ID,First name\n"
    assert "authorization" not in seen[0].headers
    assert "abc123" in str(seen[0].url)


async def test_an_unshared_sheet_reports_the_sharing_fix():
    """Google answers an unshared sheet with a sign-in PAGE and HTTP 200, not
    a 403 — so the naive path would hand the importer a lump of HTML and
    report a baffling header-row error."""
    client, _ = _client(
        lambda r: httpx.Response(200, content=b"<!DOCTYPE html><html>Sign in"),
    )
    refused(await gsheets.read_csv(client, "abc123"), External, match="not shared")


async def test_a_deleted_sheet_says_so():
    client, _ = _client(lambda r: httpx.Response(404))
    refused(await gsheets.read_csv(client, "gone"), External, match="not found")


async def test_an_implausibly_large_export_is_refused():
    client, _ = _client(
        lambda r: httpx.Response(200, content=b"x" * (gsheets.MAX_SHEET_BYTES + 1)),
    )
    refused(await gsheets.read_csv(client, "huge"), External, match="implausibly large")


# --- the write leg -----------------------------------------------------------


async def test_write_rows_clears_before_updating():
    """A roster that shrank must not leave the departed sitting below the new
    last row, so the clear is not optional."""
    client, seen = _client(lambda r: httpx.Response(200, json={}))
    ok(await gsheets.write_rows(client, "tok", "sheet1", b"ID,First name\r\n7,Ada\r\n"))
    assert [r.method for r in seen] == ["POST", "PUT"]
    assert seen[0].url.path.endswith(":clear")
    assert seen[1].url.params["valueInputOption"] == "RAW"
    assert all(r.headers["authorization"] == "Bearer tok" for r in seen)


async def test_write_rows_sends_a_matrix_not_raw_csv():
    import json

    client, seen = _client(lambda r: httpx.Response(200, json={}))
    ok(
        await gsheets.write_rows(
            client, "tok", "s", "ID,First name\r\n7,Ada\r\n".encode("utf-8-sig")
        )
    )
    body = json.loads(seen[1].content)
    assert body["values"] == [["ID", "First name"], ["7", "Ada"]], (
        "the BOM must not survive into the first cell"
    )


async def test_decoration_is_skipped_when_the_sheet_already_complies():
    client, seen = _client(lambda r: httpx.Response(200, json=_spreadsheet()))
    assert ok(await gsheets.decorate(client, "tok", "s", None)) is False
    assert len(seen) == 1, "a compliant sheet is read and left alone"


async def test_decoration_writes_when_something_is_missing():
    bare = _spreadsheet(protections=[])
    client, seen = _client(lambda r: httpx.Response(200, json=bare))
    assert ok(await gsheets.decorate(client, "tok", "s", None)) is True
    assert seen[1].url.path.endswith(":batchUpdate")


# --- retry and backoff -------------------------------------------------------


def _no_sleep(monkeypatch) -> list[float]:
    """Swallow the backoff, keeping the delays for inspection. Without this a
    single retry test would sit here for 42 seconds."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(gsheets.asyncio, "sleep", fake_sleep)
    return slept


def _statuses(*codes: int, headers: dict | None = None):
    """A handler answering the given statuses in order, 200 forever after."""
    remaining = list(codes)

    def handler(request: httpx.Request) -> httpx.Response:
        if remaining:
            return httpx.Response(remaining.pop(0), headers=headers or {}, json={})
        return httpx.Response(200, json={})

    return handler


async def test_a_rate_limited_write_is_retried_and_succeeds(monkeypatch):
    """The quota case this exists for: 429 is a refusal, so replaying the
    write is safe and the team's sync should not be recorded as failed."""
    slept = _no_sleep(monkeypatch)
    client, seen = _client(_statuses(429, 429))
    ok(await gsheets.write_rows(client, "tok", "s", b"ID\r\n"))
    assert [r.method for r in seen] == ["POST", "POST", "POST", "PUT"], (
        "two refusals, then the clear lands, then the update"
    )
    assert slept == [gsheets.RETRY_BASE_DELAY, gsheets.RETRY_BASE_DELAY * 4]


async def test_backoff_grows_and_is_capped(monkeypatch):
    """The delays must outlast a per-minute quota window, not just a blip."""
    slept = _no_sleep(monkeypatch)
    client, _ = _client(_statuses(*([429] * (gsheets.RETRY_ATTEMPTS - 1))))
    ok(await gsheets.read_csv(client, "s"))
    assert slept == [2.0, 8.0, 32.0]
    assert all(s <= gsheets.RETRY_MAX_DELAY for s in slept)


async def test_google_s_own_retry_after_wins_over_our_backoff(monkeypatch):
    slept = _no_sleep(monkeypatch)
    client, _ = _client(_statuses(429, headers={"Retry-After": "7"}))
    ok(await gsheets.read_csv(client, "s"))
    assert slept == [7.0]


async def test_an_absurd_retry_after_is_capped(monkeypatch):
    """A header we misread must not park the nightly job for an hour."""
    slept = _no_sleep(monkeypatch)
    client, _ = _client(_statuses(429, headers={"Retry-After": "3600"}))
    ok(await gsheets.read_csv(client, "s"))
    assert slept == [gsheets.RETRY_MAX_DELAY]


async def test_giving_up_reports_the_ordinary_message(monkeypatch):
    """An exhausted retry must not invent a new error shape: the last
    response comes back as-is, so the leader reads the same sentence."""
    _no_sleep(monkeypatch)
    client, seen = _client(_statuses(*([429] * 99)))
    refused(
        await gsheets.write_rows(client, "tok", "s", b"ID\r\n"),
        External,
        match="clear failed: HTTP 429",
    )
    assert len(seen) == gsheets.RETRY_ATTEMPTS


async def test_a_transport_error_becomes_a_sheet_error(monkeypatch):
    """Otherwise a reset would escape services.roster_sheets as a bare httpx
    error, and an interactive "Sync now" would die instead of reporting."""
    _no_sleep(monkeypatch)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("reset")

    client, seen = _client(boom)
    refused(
        await gsheets.read_csv(client, "s"),
        External,
        match="export failed: ConnectError",
    )
    assert len(seen) == gsheets.RETRY_ATTEMPTS, "a flaky connection is retried"


async def test_a_failed_status_is_not_retried(monkeypatch):
    """404 and 403 are answers, not "later" — retrying them just delays the
    sharing advice the leader needs."""
    _no_sleep(monkeypatch)
    client, seen = _client(_statuses(404))
    refused(await gsheets.read_csv(client, "gone"), External, match="not found")
    assert len(seen) == 1


async def test_a_sign_in_page_is_not_retried(monkeypatch):
    """It arrives as a 200, so nothing marks it retryable — but it is worth
    pinning, because retrying an unshared sheet would waste the whole quota
    window on a sheet that will never answer."""
    _no_sleep(monkeypatch)
    client, seen = _client(
        lambda r: httpx.Response(200, content=b"<!DOCTYPE html><html>Sign in"),
    )
    refused(await gsheets.read_csv(client, "s"), External, match="not shared")
    assert len(seen) == 1


# --- what must NOT be replayed -----------------------------------------------


async def test_creating_a_sheet_does_not_replay_a_5xx(monkeypatch):
    """A 500 can arrive after Drive already made the file. Retrying would
    leave the parish with two roster sheets for one team and only one of them
    recorded in team_sheet."""
    _no_sleep(monkeypatch)
    client, seen = _client(_statuses(500))
    refused(
        await gsheets.create_sheet(client, "tok", "altar-servers", folder_id="folder1"),
        External,
        match="create failed: HTTP 500",
    )
    assert len(seen) == 1


async def test_creating_a_sheet_does_retry_a_429(monkeypatch):
    """A refusal is not a half-done write, so the quota case is still
    absorbed."""
    _no_sleep(monkeypatch)
    client, seen = _client(
        lambda r: (
            httpx.Response(429, json={})
            if len(seen) == 1
            else httpx.Response(200, json={"id": "new1"})
        ),
    )
    assert (
        ok(
            await gsheets.create_sheet(
                client, "tok", "altar-servers", folder_id="folder1"
            )
        )
        == "new1"
    )
    assert len(seen) == 2


async def test_decoration_does_not_replay_its_batch_update(monkeypatch):
    """build_requests appends protected ranges and validation rules; applying
    the batch twice doubles them."""
    _no_sleep(monkeypatch)
    bare = _spreadsheet(protections=[])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(":batchUpdate"):
            return httpx.Response(503, json={})
        return httpx.Response(200, json=bare)

    client, seen = _client(handler)
    refused(
        await gsheets.decorate(client, "tok", "s", None),
        External,
        match="decoration failed: HTTP 503",
    )
    assert len(seen) == 2, "the read, then one batchUpdate that is not repeated"


async def test_a_throttling_403_is_retried(monkeypatch):
    """Drive spells quota exhaustion as a 403 with a reason as often as it
    spells it 429. Missing that would drop exactly the sync this retry
    exists to absorb."""
    slept = _no_sleep(monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                403, json={"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}
            )
        return httpx.Response(200, json={"id": "new1"})

    client, _ = _client(handler)
    assert (
        ok(
            await gsheets.create_sheet(
                client, "tok", "altar-servers", folder_id="folder1"
            )
        )
        == "new1"
    )
    assert slept == [gsheets.RETRY_BASE_DELAY], (
        "a refusal, so even a non-repeatable call may be replayed"
    )


async def test_resource_exhausted_is_retried(monkeypatch):
    """The newer error shape for the same thing."""
    slept = _no_sleep(monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(403, json={"error": {"status": "RESOURCE_EXHAUSTED"}})
        return httpx.Response(200, json={})

    client, seen = _client(handler)
    ok(await gsheets.share_anyone_writer(client, "tok", "s"))
    assert len(seen) == 2 and slept == [gsheets.RETRY_BASE_DELAY]


async def test_a_permission_403_is_reported_at_once(monkeypatch):
    """The other 403. Retrying it four times only delays the answer, and on
    the anonymous export leg it delays the sharing advice a leader needs."""
    _no_sleep(monkeypatch)
    client, seen = _client(
        lambda r: httpx.Response(
            403, json={"error": {"errors": [{"reason": "insufficientPermissions"}]}}
        ),
    )
    refused(
        await gsheets.share_anyone_writer(client, "tok", "s"),
        External,
        match="sharing failed: HTTP 403",
    )
    assert len(seen) == 1


async def test_a_403_sign_in_page_is_not_mistaken_for_throttling(monkeypatch):
    """The export endpoint answers HTML, not JSON. A body we cannot parse is
    not a quota bounce."""
    _no_sleep(monkeypatch)
    client, seen = _client(
        lambda r: httpx.Response(403, content=b"<html>Sign in</html>")
    )
    refused(await gsheets.read_csv(client, "s"), External, match="not shared")
    assert len(seen) == 1
