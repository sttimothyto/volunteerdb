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

from volunteerdb.models import ROLE_LABELS, TeamRole
from volunteerdb.services import gsheets
from volunteerdb.sheets.common import ROSTER_HEADERS

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


def _transport(monkeypatch, handler) -> list[httpx.Request]:
    """Route every AsyncClient in gsheets through a MockTransport, recording
    the requests (the idiom test_fetch_pages.py uses)."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(record)
        return real(*args, **kwargs)

    monkeypatch.setattr(gsheets.httpx, "AsyncClient", factory)
    return seen


async def test_read_csv_is_anonymous(monkeypatch):
    """No Authorization header: link sharing is the access, and requiring a
    token would put us back where the drive.file grant left us."""
    seen = _transport(
        monkeypatch, lambda r: httpx.Response(200, content=b"ID,First name\n")
    )
    assert await gsheets.read_csv("abc123") == b"ID,First name\n"
    assert "authorization" not in seen[0].headers
    assert "abc123" in str(seen[0].url)


async def test_an_unshared_sheet_reports_the_sharing_fix(monkeypatch):
    """Google answers an unshared sheet with a sign-in PAGE and HTTP 200, not
    a 403 — so the naive path would hand the importer a lump of HTML and
    report a baffling header-row error."""
    _transport(
        monkeypatch,
        lambda r: httpx.Response(200, content=b"<!DOCTYPE html><html>Sign in"),
    )
    with pytest.raises(gsheets.GSheetsError, match="not shared"):
        await gsheets.read_csv("abc123")


async def test_a_deleted_sheet_says_so(monkeypatch):
    _transport(monkeypatch, lambda r: httpx.Response(404))
    with pytest.raises(gsheets.GSheetsError, match="not found"):
        await gsheets.read_csv("gone")


async def test_an_implausibly_large_export_is_refused(monkeypatch):
    _transport(
        monkeypatch,
        lambda r: httpx.Response(200, content=b"x" * (gsheets.MAX_SHEET_BYTES + 1)),
    )
    with pytest.raises(gsheets.GSheetsError, match="implausibly large"):
        await gsheets.read_csv("huge")


# --- the write leg -----------------------------------------------------------


async def test_write_rows_clears_before_updating(monkeypatch):
    """A roster that shrank must not leave the departed sitting below the new
    last row, so the clear is not optional."""
    seen = _transport(monkeypatch, lambda r: httpx.Response(200, json={}))
    await gsheets.write_rows("tok", "sheet1", b"ID,First name\r\n7,Ada\r\n")
    assert [r.method for r in seen] == ["POST", "PUT"]
    assert seen[0].url.path.endswith(":clear")
    assert seen[1].url.params["valueInputOption"] == "RAW"
    assert all(r.headers["authorization"] == "Bearer tok" for r in seen)


async def test_write_rows_sends_a_matrix_not_raw_csv(monkeypatch):
    import json

    seen = _transport(monkeypatch, lambda r: httpx.Response(200, json={}))
    await gsheets.write_rows(
        "tok", "s", "ID,First name\r\n7,Ada\r\n".encode("utf-8-sig")
    )
    body = json.loads(seen[1].content)
    assert body["values"] == [["ID", "First name"], ["7", "Ada"]], (
        "the BOM must not survive into the first cell"
    )


async def test_decoration_is_skipped_when_the_sheet_already_complies(monkeypatch):
    seen = _transport(monkeypatch, lambda r: httpx.Response(200, json=_spreadsheet()))
    assert await gsheets.decorate("tok", "s", None) is False
    assert len(seen) == 1, "a compliant sheet is read and left alone"


async def test_decoration_writes_when_something_is_missing(monkeypatch):
    bare = _spreadsheet(protections=[])
    seen = _transport(monkeypatch, lambda r: httpx.Response(200, json=bare))
    assert await gsheets.decorate("tok", "s", None) is True
    assert seen[1].url.path.endswith(":batchUpdate")
