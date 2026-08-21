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

Raw httpx against the v3/v4 endpoints -- the same no-client-library choice as
services.gcal, whose mint_token this one mirrors -- because six verbs do not
justify a dependency tree. Every call goes through _send, which retries the
statuses Google uses to mean "later"; the Sheets quota is per-minute per-user
and a busy night's write-back can reach it.
"""

import asyncio
import csv
from io import StringIO

import httpx

from ..config import settings
from ..models import ROLE_LABELS, TeamRole
from ..sheets.common import ROSTER_HEADERS

API = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE_API = "https://www.googleapis.com/drive/v3/files"
TOKEN_URL = "https://oauth2.googleapis.com/token"
EXPORT_URL = "https://docs.google.com/spreadsheets/d/{file_id}/export?format=csv"
TIMEOUT = 20.0
# Retry policy. 429 is the one that actually bites: the Sheets quota is
# per-minute per-user, and a night where most rosters changed can brush it --
# so the delays have to outlast a quota window rather than a blip. 2s, 8s, 32s
# does; the usual 1-2-4 does not. Worst case for one call is ~42s, and a night
# that spends it on every sheet is a night that was over quota anyway: the
# backoff is what paces the job back under the limit.
RETRY_ATTEMPTS = 4
RETRY_BASE_DELAY = 2.0
RETRY_FACTOR = 4.0
RETRY_MAX_DELAY = 45.0
# 429 means the request was refused without being performed, so it is safe to
# replay whatever the call does. A 5xx may arrive *after* Google acted on it,
# which is why the two calls that are not replayable only retry on 429.
REFUSED = frozenset({429})
RETRY_STATUSES = REFUSED | frozenset({500, 502, 503, 504})
# Drive does not always spell throttling 429: it also reports it as a 403
# carrying one of these reasons. Those are refusals too, so they retry
# whatever the call is -- but only when the body says so, because the other
# 403 means "you may not touch this file" and must be reported at once.
RATE_LIMIT_REASONS = frozenset(
    {"rateLimitExceeded", "userRateLimitExceeded", "sharingRateLimitExceeded"}
)
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


class GSheetsError(RuntimeError):
    """A Google API call failed; the sync records it against the team and
    moves on to the next one."""


def _is_rate_limited(resp: httpx.Response) -> bool:
    """A 403 that is really a quota bounce rather than a permission problem.

    The distinction matters in both directions: retrying a genuine 403 just
    delays the sharing advice a leader needs, and *not* retrying a throttling
    403 wastes the sync it was meant to absorb. Only the body can tell them
    apart. A non-JSON 403 is not one of these -- the anonymous CSV export
    endpoint answers an unshared sheet with HTML, and that is the sharing case.
    """
    if resp.status_code != 403:
        return False
    try:
        error = resp.json().get("error", {})
    except ValueError:
        return False
    if error.get("status") == "RESOURCE_EXHAUSTED":
        return True
    return any(e.get("reason") in RATE_LIMIT_REASONS for e in error.get("errors", []))


def _retry_after(resp: httpx.Response, fallback: float) -> float:
    """Google's own Retry-After when it sends a usable one, else our backoff.

    Capped either way: a header we misread must not park the nightly job for
    an hour.
    """
    try:
        return min(float(resp.headers.get("Retry-After", "")), RETRY_MAX_DELAY)
    except ValueError:
        return fallback


async def _send(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    what: str,
    repeatable: bool = True,
    **kwargs,
) -> httpx.Response:
    """One request, retried while Google says "later".

    Returns the response rather than raising on a bad status: every leg here
    reports failure differently -- read_csv turns a 403 into sharing advice --
    and centralising that would flatten the messages leaders actually read.
    The last attempt's response comes back as-is, so an exhausted retry ends
    in the caller's usual "... failed: HTTP 429" instead of a new error shape.

    A throttling 403 (see _is_rate_limited) is retried like a 429 whatever
    the call, since it too was refused rather than half-performed.

    `repeatable=False` marks a call that must not be replayed after a 5xx:
    files.create and :batchUpdate both *do* something, and a 500 can arrive
    after Google already did it, leaving a duplicate sheet or a doubled
    protected range. Those still retry a 429, which is a refusal, not a
    half-done write.

    Backoff is deterministic, with no jitter: jitter spreads a herd of
    clients, and this is one nightly job -- a predictable delay is one a test
    can assert on.
    """
    statuses = RETRY_STATUSES if repeatable else REFUSED
    backoff = RETRY_BASE_DELAY
    for _ in range(RETRY_ATTEMPTS - 1):
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            # A reset or a read timeout is precisely what backoff is for. It
            # surfaces as GSheetsError so an interactive "Sync now" records it
            # against the team like any other sheet problem, rather than
            # escaping the service as a bare httpx error.
            if not repeatable:
                raise GSheetsError(f"{what} failed: {type(exc).__name__}") from exc
            wait = backoff
        else:
            if resp.status_code not in statuses and not _is_rate_limited(resp):
                return resp
            wait = _retry_after(resp, backoff)
        await asyncio.sleep(wait)
        backoff = min(backoff * RETRY_FACTOR, RETRY_MAX_DELAY)
    try:
        return await client.request(method, url, **kwargs)
    except httpx.TransportError as exc:
        raise GSheetsError(f"{what} failed: {type(exc).__name__}") from exc


def enabled() -> bool:
    s = settings()
    return bool(
        s.sheets_client_id
        and s.sheets_client_secret
        and s.sheets_refresh_token
        and s.sheets_folder_id
    )


async def mint_token() -> str:
    """A fresh access token from the stored refresh token — the same endpoint
    and the same shape as services.gcal.mint_token."""
    s = settings()
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await _send(
            client,
            "POST",
            TOKEN_URL,
            what="token mint",
            data={
                "client_id": s.sheets_client_id,
                "client_secret": s.sheets_client_secret,
                "refresh_token": s.sheets_refresh_token,
                "grant_type": "refresh_token",
            },
        )
    if resp.status_code != 200:
        raise GSheetsError(f"token mint failed: HTTP {resp.status_code}")
    return resp.json()["access_token"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def read_csv(file_id: str) -> bytes:
    """The sheet's first tab as CSV — anonymously, with no token.

    Google serves this to anybody holding the link, which is the whole point:
    a leader's own sheet needs no grant to this system, only link sharing. A
    sheet that is not shared answers with a sign-in page, not the CSV, so the
    404/HTML cases below are the ordinary "they forgot to share it" report.
    """
    url = EXPORT_URL.format(file_id=file_id)
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        resp = await _send(client, "GET", url, what="export")
    if resp.status_code == 404:
        raise GSheetsError(
            "the spreadsheet was not found — check the link, and that the "
            "sheet has not been deleted"
        )
    if resp.status_code in (401, 403):
        raise GSheetsError(
            "the spreadsheet is not shared — set it to “anyone with the "
            "link can edit” in Google Sheets’ Share dialog"
        )
    if resp.status_code != 200:
        raise GSheetsError(f"export failed: HTTP {resp.status_code}")
    content = resp.content
    if len(content) > MAX_SHEET_BYTES:
        raise GSheetsError("the spreadsheet is implausibly large for a roster")
    # An unshared sheet redirects to a sign-in page, which is a 200 of HTML.
    # Catching it here turns "Google served us a login form" into the sharing
    # message above rather than a baffling header-row parse error.
    if b"<html" in content[:1024].lower():
        raise GSheetsError(
            "the spreadsheet is not shared — Google returned a sign-in page "
            "instead of the roster. Set it to “anyone with the link can edit”."
        )
    return content


def _csv_to_values(content: bytes) -> list[list[str]]:
    """CSV bytes as the row-major matrix the Sheets API wants. utf-8-sig
    because the exporter writes a BOM for Excel's sake."""
    return [row for row in csv.reader(StringIO(content.decode("utf-8-sig")))]


async def write_rows(token: str, file_id: str, content: bytes) -> None:
    """Replace the first tab's contents with `content`.

    Clear-then-update, not update alone: a roster that shrank would otherwise
    leave the departed members' rows sitting below the new last row.
    RAW, so a phone number like +1-416-555-0100 is not parsed as a formula
    (the exporter's own injection guard assumes the value survives intact).
    """
    values = _csv_to_values(content)
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        clear = await _send(
            client,
            "POST",
            f"{API}/{file_id}/values/A1:ZZ:clear",
            what="clear",
            headers=_headers(token),
            json={},
        )
        if clear.status_code != 200:
            raise GSheetsError(f"clear failed: HTTP {clear.status_code}")
        resp = await _send(
            client,
            "PUT",
            f"{API}/{file_id}/values/A1",
            what="write",
            headers=_headers(token),
            params={"valueInputOption": "RAW"},
            json={"values": values},
        )
    if resp.status_code != 200:
        raise GSheetsError(f"write failed: HTTP {resp.status_code}")


async def create_sheet(token: str, title: str) -> str:
    """A new spreadsheet in the parish roster folder; returns its file id.

    Drive rather than Sheets: only files.create takes a parent folder, and a
    sheet created by this client is covered by the drive.file half of the
    grant, which is what lets share_anyone_writer touch it afterwards.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await _send(
            client,
            "POST",
            DRIVE_API,
            what="create",
            repeatable=False,
            headers=_headers(token),
            json={
                "name": title,
                "mimeType": "application/vnd.google-apps.spreadsheet",
                "parents": [settings().sheets_folder_id],
            },
            params={"fields": "id"},
        )
    if resp.status_code not in (200, 201):
        raise GSheetsError(f"create failed: HTTP {resp.status_code}")
    return resp.json()["id"]


async def share_anyone_writer(token: str, file_id: str) -> None:
    """Link sharing, editor role — what replaces the retired per-leader Drive
    share leg. A leader needs no Google grant of their own now; holding the
    link is the access, which is exactly why the team page warns that the
    link must be kept private."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await _send(
            client,
            "POST",
            f"{DRIVE_API}/{file_id}/permissions",
            what="sharing",
            headers=_headers(token),
            json={"type": "anyone", "role": "writer"},
        )
    if resp.status_code not in (200, 201):
        raise GSheetsError(f"sharing failed: HTTP {resp.status_code}")


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


async def decorate(token: str, file_id: str, team_values: list[str] | None) -> bool:
    """Re-apply the leader-facing decoration; True if anything was written.

    Skipped when the sheet already complies, so Drive version history stays
    quiet and a leader opening the file does not see a nightly robot edit.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await _send(
            client,
            "GET",
            f"{API}/{file_id}",
            what="read for decoration",
            headers=_headers(token),
            params={"ranges": "A1:H2", "fields": GET_FIELDS},
        )
        if resp.status_code != 200:
            raise GSheetsError(f"read for decoration failed: HTTP {resp.status_code}")
        state = first_sheet_state(resp.json())
        if is_decorated(state, team_values):
            return False
        batch = await _send(
            client,
            "POST",
            f"{API}/{file_id}:batchUpdate",
            what="decoration",
            repeatable=False,
            headers=_headers(token),
            json={"requests": build_requests(state, team_values)},
        )
    if batch.status_code != 200:
        raise GSheetsError(f"decoration failed: HTTP {batch.status_code}")
    return True
