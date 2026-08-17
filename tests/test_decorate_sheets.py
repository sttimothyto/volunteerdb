"""The host-side sheet-decoration/sharing script (deploy/files/, runs outside
the container with no venv). Tested here: the constants that must track the
app (role labels, column positions), the pure request builder and its safety
property (field masks that cannot reach cell values), state distillation, the
Team dropdown resolved from the manifest, the access reconciler and its
conservatism, rclone-conf parsing, and SERVICE_DISABLED detection. NOT tested,
mirroring test_drive_sync.py: the live token mint, the Sheets and Drive APIs
themselves, and the rclone listing — those are covered by the runbook in
docs/how-to/drive-roster-sync.md."""

import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

from volunteerdb.models import ROLE_LABELS, TeamRole
from volunteerdb.sheets.common import ROSTER_HEADERS

_SCRIPT = (
    Path(__file__).parent.parent / "deploy" / "files" / "volunteerdb-decorate-sheets.py"
)
_spec = importlib.util.spec_from_file_location("decorate_sheets", _SCRIPT)
decorate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(decorate)


# --- constants must track the app (the host cannot import volunteerdb) -------


def test_role_labels_match_the_app_enum():
    assert decorate.ROLE_LABELS == [ROLE_LABELS[role] for role in TeamRole], (
        "the dropdown must byte-match what the exporter writes, in enum order"
    )


def test_columns_match_the_roster_header():
    assert decorate.ROLE_COLUMN == ROSTER_HEADERS.index("Role")
    assert decorate.TEAM_COLUMN == ROSTER_HEADERS.index("Team")
    assert decorate.ID_COLUMN == ROSTER_HEADERS.index("ID")


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
    note = decorate.WARNING_NOTE if note is None else note
    labels = decorate.ROLE_LABELS if labels is None else labels
    if protections is None:
        protections = [
            {
                "protectedRangeId": 9,
                "description": decorate.PROTECTION_MARKER,
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
    state = decorate.first_sheet_state(_spreadsheet())
    assert state == _state()
    assert decorate.is_decorated(state)


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
        assert not decorate.is_decorated(_state(**broken)), broken


def test_a_lenient_dropdown_is_not_a_dropdown():
    """strict=False lets a leader type anything past the list — the whole
    point of the rule is that the sync never sees an unknown value."""
    sheet = _spreadsheet()
    row = sheet["sheets"][0]["data"][0]["rowData"][1]["values"]
    row[decorate.ROLE_COLUMN]["dataValidation"]["strict"] = False
    assert not decorate.first_sheet_state(sheet)["dropdown_ok"]


# --- the Team dropdown -------------------------------------------------------


_MANIFEST = {
    "teams": ["Choir", "Choir / Choir (Sun 10am)", "Ushers"],
    "sheets": {
        "choir-choir-sun-10am-membership-list": {
            "team": "Choir / Choir (Sun 10am)",
            "editors": ["cantor@gmail.com"],
        }
    },
}


def test_a_team_sheet_offers_only_its_own_path():
    assert decorate.team_values_for(
        "choir-choir-sun-10am-membership-list", _MANIFEST
    ) == ["Choir / Choir (Sun 10am)"]


def test_the_template_offers_every_team():
    name = "ROSTER TEMPLATE (copy me, do not edit)"
    assert decorate.team_values_for(name, _MANIFEST) == _MANIFEST["teams"]


def test_unknown_and_manifestless_sheets_keep_whatever_they_have():
    assert decorate.team_values_for("archived-team-membership-list", _MANIFEST) is None
    assert (
        decorate.team_values_for("choir-choir-sun-10am-membership-list", None) is None
    )


def test_an_oversized_team_list_skips_the_rule_rather_than_failing():
    huge = {"teams": [f"Team {i}" for i in range(decorate.MAX_VALIDATION_VALUES + 1)]}
    assert decorate.team_values_for("ROSTER TEMPLATE", huge) is None


def test_a_renamed_team_forces_redecoration():
    """The Team dropdown carries a team's display path, so a rename must show
    up as non-compliance — otherwise the sheet offers a name that no longer
    resolves and every row fails the next sync."""
    state = decorate.first_sheet_state(_spreadsheet(teams=["Choir / Choir (Sun 8am)"]))
    assert decorate.is_decorated(state, ["Choir / Choir (Sun 8am)"])
    assert not decorate.is_decorated(state, ["Choir / Choir (Sun 08:00)"])
    assert decorate.is_decorated(state, None), "None means leave the column alone"


def test_freshly_converted_sheet_reads_as_undecorated():
    """What a sheet looks like right after the rclone convert-on-upload:
    no notes, no validation, nothing frozen or hidden, no protections."""
    bare = {
        "sheets": [
            {
                "properties": {"sheetId": 0, "gridProperties": {"rowCount": 14}},
                "data": [{"rowData": [{"values": [{}]}], "columnMetadata": [{}]}],
            }
        ]
    }
    state = decorate.first_sheet_state(bare)
    assert not decorate.is_decorated(state)
    assert state["marker_protections"] == []


# --- the request builder -----------------------------------------------------


def test_build_requests_golden():
    requests = decorate.build_requests(_state(marker_protections=[]), ["Choir"])
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
    ] == decorate.ROLE_LABELS
    assert validation["rule"]["strict"] is True
    team = requests[1]["setDataValidation"]
    assert team["range"]["startColumnIndex"] == 6
    assert team["range"]["endColumnIndex"] == 7
    assert [v["userEnteredValue"] for v in team["rule"]["condition"]["values"]] == [
        "Choir"
    ]
    assert team["rule"]["strict"] is True
    notes = requests[2]["updateCells"]
    assert notes["rows"] == [
        {
            "values": [
                {"note": decorate.WARNING_NOTE},
                {"note": decorate.WARNING_NOTE},  # B1 too: A1 hides with column A
            ]
        }
    ]
    protection = requests[5]["addProtectedRange"]["protectedRange"]
    assert protection["warningOnly"] is True
    assert protection["description"] == decorate.PROTECTION_MARKER


def test_no_team_values_means_no_team_request():
    requests = decorate.build_requests(_state(marker_protections=[]))
    validations = [r for r in requests if "setDataValidation" in r]
    assert len(validations) == 1
    assert validations[0]["setDataValidation"]["range"]["startColumnIndex"] == 7


def test_field_masks_cannot_reach_cell_values():
    """The safety property: every masked request touches metadata only."""
    requests = decorate.build_requests(_state(marker_protections=[]), ["Choir"])
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
    requests = decorate.build_requests(_state(row_count=1))
    assert next(iter(requests[0])) == "appendDimension", (
        "must come first: setDataValidation on row 2 of a 1-row grid errors"
    )
    assert requests[0]["appendDimension"]["length"] == decorate.BLANK_ROWS


def test_duplicate_marker_protections_are_pruned_not_readded():
    requests = decorate.build_requests(_state(marker_protections=[3, 8, 11]))
    kinds = [next(iter(r)) for r in requests]
    assert "addProtectedRange" not in kinds
    deletes = [
        r["deleteProtectedRange"]["protectedRangeId"]
        for r in requests
        if "deleteProtectedRange" in r
    ]
    assert deletes == [8, 11], "the first survives; crash debris goes"


# --- access reconciliation ---------------------------------------------------


_PERMISSIONS = [
    {"id": "anyoneWithLink", "type": "anyone", "role": "writer"},
    {
        "id": "1",
        "type": "user",
        "role": "owner",
        "emailAddress": "admin@sttimothyto.org",
    },
    {
        "id": "2",
        "type": "user",
        "role": "writer",
        "emailAddress": "pastor@sttimothyto.org",
        "permissionDetails": [{"permissionType": "file", "inherited": True}],
    },
    {"id": "3", "type": "user", "role": "writer", "emailAddress": "Cantor@Gmail.com"},
]


def test_only_missing_editors_are_granted():
    add, remove, blocked = decorate.share_plan(
        _PERMISSIONS, ["cantor@gmail.com", "deacon@hotmail.com"], revoke_links=False
    )
    assert add == ["deacon@hotmail.com"], "already-shared leaders are matched casefold"
    assert (remove, blocked) == ([], [])


def test_a_second_pass_does_nothing():
    granted = _PERMISSIONS + [
        {"id": "4", "type": "user", "role": "writer", "emailAddress": "d@hotmail.com"}
    ]
    assert decorate.share_plan(granted, ["d@hotmail.com"], revoke_links=True)[0] == []


def test_links_go_only_when_asked_and_nobody_else_is_touched():
    _add, remove, blocked = decorate.share_plan(_PERMISSIONS, [], revoke_links=False)
    assert (remove, blocked) == ([], [])
    _add, remove, blocked = decorate.share_plan(_PERMISSIONS, [], revoke_links=True)
    assert remove == ["anyoneWithLink"], (
        "the owner, the inherited pastor grant and the hand-added editor all stay"
    )
    assert blocked == []


def test_a_domain_grant_is_a_public_link_by_another_name():
    perms = [{"id": "d", "type": "domain", "role": "writer", "domain": "example.org"}]
    assert decorate.share_plan(perms, [], revoke_links=True)[1] == ["d"]


def test_an_inherited_link_is_reported_not_attempted():
    """Drive refuses to delete a folder-level grant from one file; deleting it
    anyway would fail every sheet nightly instead of naming the real fix."""
    perms = [
        {
            "id": "anyoneWithLink",
            "type": "anyone",
            "role": "writer",
            "permissionDetails": [{"permissionType": "member", "inherited": True}],
        }
    ]
    _add, remove, blocked = decorate.share_plan(perms, [], revoke_links=True)
    assert (remove, blocked) == ([], ["anyoneWithLink"])


def test_a_family_address_shared_by_two_leaders_is_invited_once():
    add, _remove, _blocked = decorate.share_plan(
        _PERMISSIONS, ["Both@example.org", "both@example.org  "], revoke_links=False
    )
    assert add == ["both@example.org"]


def test_a_team_with_no_editors_yields_no_operations():
    assert decorate.share_plan(_PERMISSIONS, [], revoke_links=False) == ([], [], [])


# --- the manifest ------------------------------------------------------------


def test_manifest_round_trip(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_MANIFEST), encoding="utf-8")
    assert decorate.load_manifest(str(path)) == _MANIFEST
    assert decorate.load_manifest(None) is None


def test_a_broken_manifest_stops_the_run(tmp_path):
    """Never fall through to decorate-only on a corrupt manifest: that would
    silently skip a night's sharing with a zero exit status."""
    path = tmp_path / "manifest.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit):
        decorate.load_manifest(str(path))
    with pytest.raises(SystemExit):
        decorate.load_manifest(str(tmp_path / "absent.json"))


# --- rclone.conf parsing -----------------------------------------------------


def test_oauth_from_conf(tmp_path):
    conf = tmp_path / "rclone.conf"
    conf.write_text(
        "[volunteerdb-gdrive-backup]\n"
        "type = drive\n"
        "client_id = abc.apps.googleusercontent.com\n"
        "client_secret = hush\n"
        "scope = drive.file\n"
        'token = {"access_token":"stale","refresh_token":"refresh-me"}\n'
    )
    assert decorate.oauth_from_conf(str(conf), "volunteerdb-gdrive-backup") == (
        "abc.apps.googleusercontent.com",
        "hush",
        "refresh-me",
    )
    with pytest.raises(SystemExit):
        decorate.oauth_from_conf(str(conf), "no-such-remote")


# --- SERVICE_DISABLED detection ----------------------------------------------


def test_service_disabled_aborts_with_activation_url(monkeypatch):
    activation = (
        "https://console.developers.google.com/apis/api/"
        "sheets.googleapis.com/overview?project=42"
    )
    body = json.dumps(
        {
            "error": {
                "status": "PERMISSION_DENIED",
                "details": [
                    {
                        "reason": "SERVICE_DISABLED",
                        "metadata": {"activationUrl": activation},
                    }
                ],
            }
        }
    ).encode()

    def refuse(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 403, "Forbidden", {}, io.BytesIO(body)
        )

    monkeypatch.setattr(decorate.urllib.request, "urlopen", refuse)
    with pytest.raises(decorate.ServiceDisabled) as err:
        decorate.api_call("token", "https://sheets.googleapis.com/v4/spreadsheets/x")
    assert activation in str(err.value)


def test_timeout_is_retried(monkeypatch):
    """A per-file backend stall times out once, then answers — seen live on
    prod (the slow request warms the file). The retry must absorb it."""
    calls = {"n": 0}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"ok": true}'

    def flaky(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("The read operation timed out")
        return _Resp()

    monkeypatch.setattr(decorate.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(decorate.time, "sleep", lambda s: None)
    result = decorate.api_call("token", "https://sheets.googleapis.com/v4/x")
    assert result == {"ok": True}
    assert calls["n"] == 2
