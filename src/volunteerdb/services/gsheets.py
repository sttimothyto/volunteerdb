"""Google Sheets rosters, over raw REST.

A team's roster spreadsheet is an ordinary Google Sheet shared "anyone with
the link can edit". That sharing is what makes this module possible at all:
the previous transport was rclone on the host under a drive.file grant, which
by construction sees only files this system created, so a sheet a leader made
in their own Drive could never be reached. A link-shared sheet, by contrast,
is readable with no credentials at all and writable by any token carrying the
spreadsheets scope -- wherever the file lives and whoever owns it.

So the two legs are deliberately asymmetric:

    read   anonymous GET of the CSV export endpoint, exactly the way
           services.pages fetches a published Google Doc. No token, no quota,
           and it works on a sheet shared read-only.
    write  Sheets v4 values.update under the parish account's token.

Raw httpx against the v3/v4 endpoints, under the parish Google token that
services.gcal shares (services/google_api.py holds the token mint and the
retry policy); no client library, because six verbs do not justify a
dependency tree. Every call goes through _send, which retries the statuses
Google uses to mean "later"; the Sheets quota is per-minute per-user and a
busy night's write-back can reach it.

The client and the configuration are the caller's (env.Env.http, env.Env
.google()), and a failed call comes back as an Err[External] carrying the
sentence the team page shows -- the sync records it against the team and
moves on to the next one. Nothing here raises.
"""

import asyncio  # noqa: F401 -- the retry tests patch asyncio.sleep through this name
import csv
from io import StringIO

import httpx

from ..errors import External
from ..fp import Err, Ok, Result
from ..models import ROLE_LABELS, TeamRole
from ..sheets.common import ROSTER_HEADERS
from . import google_api
from .google_api import (  # noqa: F401 -- re-exported: the retry policy is this module's contract too
    RATE_LIMIT_REASONS,
    REFUSED,
    RETRY_ATTEMPTS,
    RETRY_BASE_DELAY,
    RETRY_FACTOR,
    RETRY_MAX_DELAY,
    RETRY_STATUSES,
    TIMEOUT,
    TOKEN_URL,
    GoogleConfig,
)

SERVICE = "google sheets"
API = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE_API = "https://www.googleapis.com/drive/v3/files"
EXPORT_URL = "https://docs.google.com/spreadsheets/d/{file_id}/export?format=csv"
# A roster CSV is KBs; anything near this is not a roster. Carried over from
# the retired jobs/drive_sync, where it guarded the same import.
MAX_SHEET_BYTES = 5_000_000

# Decoration, ported from the retired host-side decorate-sheets script. The
# column indexes are positions in ROSTER_HEADERS, asserted below so a change
# to the roster layout cannot silently point the dropdowns at other columns.
ID_COLUMN = ROSTER_HEADERS.index("ID")
TEAM_COLUMN = ROSTER_HEADERS.index("Team")
ROLE_COLUMN = ROSTER_HEADERS.index("Role")
PROTECTION_MARKER = "volunteerdb-sync"
# Sheets caps a ONE_OF_LIST rule; past it, skip the Team rule rather than fail
# the sheet (the parish would need 500 teams to get here).
MAX_VALIDATION_VALUES = 500
BLANK_ROWS = 25  # so the open-ended dropdown range has somewhere to exist
WARNING_NOTE = (
    "Synced nightly with VolunteerDB. Do not add, remove, reorder or rename "
    "columns, and do not edit this header row — edit member rows only. Need "
    "to restructure or scratch-edit? Work on a COPY (File → Make a copy) — "
    "copies are never synced. Don't add extra tabs here: the nightly rewrite "
    "can delete them."
)
GET_FIELDS = (
    "sheets(properties(sheetId,gridProperties(rowCount,frozenRowCount)),"
    "protectedRanges(protectedRangeId,description,warningOnly),"
    "data(rowData(values(note,dataValidation)),columnMetadata(hiddenByUser)))"
)
ROLE_VALUES = [ROLE_LABELS[role] for role in TeamRole]


def _err(message: str) -> Err[External]:
    return Err(External(SERVICE, message))


def _failed(what: str, resp: httpx.Response) -> Err[External]:
    return google_api.failed(what, f"HTTP {resp.status_code}", service=SERVICE)


async def _send(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    what: str,
    repeatable: bool = True,
    **kwargs,
) -> Result[httpx.Response, External]:
    """google_api.send under this module's name, so a transport failure is
    recorded against the team like any other sheet problem."""
    return await google_api.send(
        client, method, url, what=what, repeatable=repeatable, service=SERVICE, **kwargs
    )


def enabled(cfg: GoogleConfig) -> bool:
    """The parish token, plus the roster folder new sheets are made in."""
    return cfg.configured and bool(cfg.folder_id)


async def mint_token(
    client: httpx.AsyncClient, cfg: GoogleConfig
) -> Result[str, External]:
    """A fresh access token from the parish token (google_api.mint_token)."""
    return await google_api.mint_token(client, cfg, service=SERVICE)


_headers = google_api.headers


async def read_csv(client: httpx.AsyncClient, file_id: str) -> Result[bytes, External]:
    """The sheet's first tab as CSV — anonymously, with no token.

    Google serves this to anybody holding the link, which is the whole point:
    a leader's own sheet needs no grant to this system, only link sharing. A
    sheet that is not shared answers with a sign-in page, not the CSV, so the
    404/HTML cases below are the ordinary "they forgot to share it" report.
    The client must follow redirects (env.HttpClients.client(
    follow_redirects=True)): the export endpoint answers with one.
    """
    url = EXPORT_URL.format(file_id=file_id)
    sent = await _send(client, "GET", url, what="export")
    if isinstance(sent, Err):
        return sent
    resp = sent.value
    if resp.status_code == 404:
        return _err(
            "the spreadsheet was not found — check the link, and that the "
            "sheet has not been deleted"
        )
    if resp.status_code in (401, 403):
        return _err(
            "the spreadsheet is not shared — set it to “anyone with the "
            "link can edit” in Google Sheets’ Share dialog"
        )
    if resp.status_code != 200:
        return _failed("export", resp)
    content = resp.content
    if len(content) > MAX_SHEET_BYTES:
        return _err("the spreadsheet is implausibly large for a roster")
    # An unshared sheet redirects to a sign-in page, which is a 200 of HTML.
    # Catching it here turns "Google served us a login form" into the sharing
    # message above rather than a baffling header-row parse error.
    if b"<html" in content[:1024].lower():
        return _err(
            "the spreadsheet is not shared — Google returned a sign-in page "
            "instead of the roster. Set it to “anyone with the link can edit”."
        )
    return Ok(content)


def _csv_to_values(content: bytes) -> list[list[str]]:
    """CSV bytes as the row-major matrix the Sheets API wants. utf-8-sig
    because the exporter writes a BOM for Excel's sake."""
    return [row for row in csv.reader(StringIO(content.decode("utf-8-sig")))]


async def write_rows(
    client: httpx.AsyncClient, token: str, file_id: str, content: bytes
) -> Result[None, External]:
    """Replace the first tab's contents with `content`.

    Clear-then-update, not update alone: a roster that shrank would otherwise
    leave the departed members' rows sitting below the new last row.
    RAW, so a phone number like +1-416-555-0100 is not parsed as a formula
    (the exporter's own injection guard assumes the value survives intact).
    """
    values = _csv_to_values(content)
    clear = await _send(
        client,
        "POST",
        f"{API}/{file_id}/values/A1:ZZ:clear",
        what="clear",
        headers=_headers(token),
        json={},
    )
    if isinstance(clear, Err):
        return clear
    if clear.value.status_code != 200:
        return _failed("clear", clear.value)
    sent = await _send(
        client,
        "PUT",
        f"{API}/{file_id}/values/A1",
        what="write",
        headers=_headers(token),
        params={"valueInputOption": "RAW"},
        json={"values": values},
    )
    if isinstance(sent, Err):
        return sent
    if sent.value.status_code != 200:
        return _failed("write", sent.value)
    return Ok(None)


async def create_sheet(
    client: httpx.AsyncClient, token: str, title: str, *, folder_id: str
) -> Result[str, External]:
    """A new spreadsheet in the parish roster folder; returns its file id.

    Drive rather than Sheets: only files.create takes a parent folder, and a
    sheet created by this client is covered by the drive.file half of the
    grant, which is what lets share_anyone_writer touch it afterwards.
    """
    sent = await _send(
        client,
        "POST",
        DRIVE_API,
        what="create",
        repeatable=False,
        headers=_headers(token),
        json={
            "name": title,
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "parents": [folder_id],
        },
        params={"fields": "id"},
    )
    if isinstance(sent, Err):
        return sent
    if sent.value.status_code not in (200, 201):
        return _failed("create", sent.value)
    return Ok(sent.value.json()["id"])


async def share_anyone_writer(
    client: httpx.AsyncClient, token: str, file_id: str
) -> Result[None, External]:
    """Link sharing, editor role — what replaces the retired per-leader Drive
    share leg. A leader needs no Google grant of their own now; holding the
    link is the access, which is exactly why the team page warns that the
    link must be kept private."""
    sent = await _send(
        client,
        "POST",
        f"{DRIVE_API}/{file_id}/permissions",
        what="sharing",
        headers=_headers(token),
        json={"type": "anyone", "role": "writer"},
    )
    if isinstance(sent, Err):
        return sent
    if sent.value.status_code not in (200, 201):
        return _failed("sharing", sent.value)
    return Ok(None)


def _dropdown_values(cell: dict) -> list[str] | None:
    """The strict ONE_OF_LIST values on a cell, or None where it has no such
    rule — a missing or lenient rule is not the dropdown we mean to install."""
    validation = cell.get("dataValidation", {})
    condition = validation.get("condition", {})
    if condition.get("type") != "ONE_OF_LIST" or validation.get("strict") is not True:
        return None
    return [v.get("userEnteredValue") for v in condition.get("values", [])]


def _validation_request(sheet_id: int, column: int, values: list[str]) -> dict:
    """A strict dropdown over every data row of one column. Blank cells stay
    legal — Sheets validation never rejects an empty cell, which is what the
    importer expects of a Team column in a team's own sheet."""
    return {
        "setDataValidation": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,  # open-ended: every data row
                "startColumnIndex": column,
                "endColumnIndex": column + 1,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": v} for v in values],
                },
                "strict": True,
                "showCustomUi": True,
            },
        }
    }


def first_sheet_state(spreadsheet: dict) -> dict:
    """Distill the first tab into the facts is_decorated/build_requests need."""
    sheet = spreadsheet["sheets"][0]
    props = sheet["properties"]
    grid = sheet.get("data", [{}])[0]
    rows = grid.get("rowData", [])

    def cell(row: int, col: int) -> dict:
        try:
            return rows[row].get("values", [])[col]
        except IndexError:
            return {}

    columns = grid.get("columnMetadata", [])
    return {
        "sheet_id": props["sheetId"],
        "row_count": props.get("gridProperties", {}).get("rowCount", 0),
        "frozen": props.get("gridProperties", {}).get("frozenRowCount", 0) >= 1,
        "id_hidden": bool(columns and columns[ID_COLUMN].get("hiddenByUser")),
        "notes_ok": all(cell(0, col).get("note") == WARNING_NOTE for col in (0, 1)),
        "dropdown_ok": _dropdown_values(cell(1, ROLE_COLUMN)) == ROLE_VALUES,
        "team_values": _dropdown_values(cell(1, TEAM_COLUMN)),
        "marker_protections": [
            p["protectedRangeId"]
            for p in sheet.get("protectedRanges", [])
            if p.get("description") == PROTECTION_MARKER and p.get("warningOnly")
        ],
    }


def is_decorated(state: dict, team_values: list[str] | None = None) -> bool:
    """team_values None means the caller could not resolve a Team dropdown for
    this sheet; whatever it already carries in that column is left as it is."""
    return (
        state["row_count"] >= 2
        and state["frozen"]
        and state["id_hidden"]
        and state["notes_ok"]
        and state["dropdown_ok"]
        and (team_values is None or state["team_values"] == team_values)
        and len(state["marker_protections"]) == 1
    )


def build_requests(state: dict, team_values: list[str] | None = None) -> list[dict]:
    """One atomic batchUpdate. Every request is either values-free by nature
    or fields-masked so it cannot reach cell values."""
    sid = state["sheet_id"]
    requests: list[dict] = []
    if state["row_count"] < 2:
        requests.append(
            {
                "appendDimension": {
                    "sheetId": sid,
                    "dimension": "ROWS",
                    "length": BLANK_ROWS,
                }
            }
        )
    requests.append(_validation_request(sid, ROLE_COLUMN, ROLE_VALUES))
    if team_values is not None and len(team_values) <= MAX_VALIDATION_VALUES:
        requests.append(_validation_request(sid, TEAM_COLUMN, team_values))
    requests.append(
        {
            "updateCells": {
                "start": {"sheetId": sid, "rowIndex": 0, "columnIndex": 0},
                # A1 AND B1: A1 alone is invisible once column A hides
                "rows": [{"values": [{"note": WARNING_NOTE}, {"note": WARNING_NOTE}]}],
                "fields": "note",
            }
        }
    )
    requests.append(
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        }
    )
    requests.append(
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sid,
                    "dimension": "COLUMNS",
                    "startIndex": ID_COLUMN,
                    "endIndex": ID_COLUMN + 1,
                },
                "properties": {"hiddenByUser": True},
                "fields": "hiddenByUser",
            }
        }
    )
    marker = state["marker_protections"]
    if not marker:
        requests.append(
            {
                "addProtectedRange": {
                    "protectedRange": {
                        "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                        "description": PROTECTION_MARKER,
                        "warningOnly": True,
                    }
                }
            }
        )
    else:  # not overwrite-idempotent: drop crash debris beyond the first
        requests.extend(
            {"deleteProtectedRange": {"protectedRangeId": pid}} for pid in marker[1:]
        )
    return requests


async def decorate(
    client: httpx.AsyncClient,
    token: str,
    file_id: str,
    team_values: list[str] | None,
) -> Result[bool, External]:
    """Re-apply the leader-facing decoration; True if anything was written.

    Skipped when the sheet already complies, so Drive version history stays
    quiet and a leader opening the file does not see a nightly robot edit.
    """
    read = await _send(
        client,
        "GET",
        f"{API}/{file_id}",
        what="read for decoration",
        headers=_headers(token),
        params={"ranges": "A1:H2", "fields": GET_FIELDS},
    )
    if isinstance(read, Err):
        return read
    if read.value.status_code != 200:
        return _failed("read for decoration", read.value)
    state = first_sheet_state(read.value.json())
    if is_decorated(state, team_values):
        return Ok(False)
    batch = await _send(
        client,
        "POST",
        f"{API}/{file_id}:batchUpdate",
        what="decoration",
        repeatable=False,
        headers=_headers(token),
        json={"requests": build_requests(state, team_values)},
    )
    if isinstance(batch, Err):
        return batch
    if batch.value.status_code != 200:
        return _failed("decoration", batch.value)
    return Ok(True)
