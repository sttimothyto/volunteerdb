#!/usr/bin/env python3
"""Re-apply leader-facing decoration to every roster sheet on Drive.

Convert-on-upload (the sync's rclone leg) replaces a spreadsheet's whole
body, wiping validation, notes, hidden columns and protections — so this
runs as a nightly self-healing leg of volunteerdb-drive-sync, after the
upload loop and BEFORE the relist that `record` reads: decoration bumps
Drive ModTime, and the stored sync marks must postdate it or every sheet
looks leader-edited the next night.

Per sheet (first tab): Role column dropdown of the four role labels
(strict), a structure warning as a note on A1+B1, frozen header row,
hidden ID column, and a warning-only protection on the header. All
requests are metadata/format-only by field-mask construction — they
cannot touch cell values. Sheets already compliant are skipped so Drive
version history stays quiet.

Runs on the HOST (stdlib only): rclone lists the folder; auth reuses the
rclone remote's OAuth client by minting an access token from its refresh
token (rclone.conf is never written). Requires the Google Sheets API to
be enabled for that client's Cloud project — a SERVICE_DISABLED abort
prints the activation URL.
"""

import argparse
import configparser
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Must equal [ROLE_LABELS[r] for r in TeamRole] from volunteerdb.models, in
# enum order, and byte-match what the exporter writes — guarded by
# tests/test_decorate_sheets.py (the host has no venv to import the app from).
ROLE_LABELS = ["Ministry leader", "Second-in-command", "Core team member", "Member"]
ROLE_COLUMN = 7  # 0-based H; ROSTER_HEADERS.index("Role")
ID_COLUMN = 0  # column A; ROSTER_HEADERS.index("ID")
PROTECTION_MARKER = "volunteerdb-sync"
WARNING_NOTE = (
    "Synced nightly with VolunteerDB. Do not add, remove, reorder or rename "
    "columns, and do not edit this header row — edit member rows only. Need "
    "to restructure or scratch-edit? Work on a COPY (File → Make a copy) — "
    "copies are never synced. Don't add extra tabs here: the nightly rewrite "
    "can delete them."
)
# Rows to give a fresh header-only sheet (the template, an empty team) so the
# open-ended dropdown range below has somewhere to exist.
BLANK_ROWS = 25

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GET_FIELDS = (
    "sheets(properties(sheetId,gridProperties(rowCount,frozenRowCount)),"
    "protectedRanges(protectedRangeId,description,warningOnly),"
    "data(rowData(values(note,dataValidation)),columnMetadata(hiddenByUser)))"
)


class ServiceDisabled(Exception):
    """Sheets API not enabled for the OAuth client's Cloud project."""


def oauth_from_conf(conf_path: str, remote: str) -> tuple[str, str, str]:
    """(client_id, client_secret, refresh_token) from the rclone remote."""
    conf = configparser.ConfigParser()
    if not conf.read(conf_path):
        raise SystemExit(f"cannot read rclone config {conf_path!r}")
    if remote not in conf:
        raise SystemExit(f"remote [{remote}] not in {conf_path!r}")
    section = conf[remote]
    token = json.loads(section["token"])
    return section["client_id"], section["client_secret"], token["refresh_token"]


def mint_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Fresh access token; never writes anything back to rclone.conf."""
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode()
    with urllib.request.urlopen(TOKEN_URL, data=body, timeout=30) as resp:
        return json.loads(resp.read())["access_token"]


def list_spreadsheets(
    conf_path: str, remote: str, folder: str
) -> list[tuple[str, str]]:
    """(name, file_id) for the folder's roster sheets and template only —
    leader scratch copies ("Copy of ...") are deliberately left untouched.
    Listed with csv export formats like the sync's legs, so Google-native
    sheets appear with a .csv suffix (rclone names Docs by export format)."""
    out = subprocess.run(
        [
            "rclone",
            "--config",
            conf_path,
            "--drive-export-formats",
            "csv",
            "lsjson",
            f"{remote}:{folder}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    sheets = []
    for item in json.loads(out):
        if item.get("IsDir") or not item["Name"].endswith(".csv"):
            continue
        name = item["Name"].removesuffix(".csv")
        if name.endswith("-membership-list") or "TEMPLATE" in name:
            sheets.append((name, item["ID"]))
    return sheets


def api_call(token: str, url: str, payload: dict | None = None) -> dict:
    """GET (payload None) or POST with retry on 429/5xx; SERVICE_DISABLED
    raises immediately — every subsequent sheet would fail the same way."""
    for attempt in range(3):
        req = urllib.request.Request(
            url,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as err:
            body = err.read().decode(errors="replace")
            if "SERVICE_DISABLED" in body:
                url_hint = ""
                try:
                    for detail in json.loads(body)["error"]["details"]:
                        url_hint = detail.get("metadata", {}).get("activationUrl", "")
                        if url_hint:
                            break
                except (ValueError, KeyError):
                    pass
                raise ServiceDisabled(
                    "Google Sheets API is not enabled for this project. "
                    f"Enable it once, then re-run: {url_hint or body[:400]}"
                ) from None
            # Google reports minute-window quota hits as 429 OR 403 — match
            # the body, and wait long enough for the window to roll over.
            rate_limited = "RATE_LIMIT_EXCEEDED" in body or "rateLimitExceeded" in body
            if (rate_limited or err.code in (429, 500, 502, 503)) and attempt < 2:
                retry_after = err.headers.get("Retry-After")
                if retry_after:
                    time.sleep(float(retry_after))
                else:
                    time.sleep(30 * (attempt + 1) if rate_limited else 2**attempt * 5)
                continue
            raise RuntimeError(f"HTTP {err.code}: {body[:400]}") from None
        except (TimeoutError, urllib.error.URLError) as err:
            # A per-file backend stall: the slow request itself warms the
            # file, so the retry after it tends to answer instantly.
            if attempt < 2:
                time.sleep(2**attempt * 5)
                continue
            raise RuntimeError(f"no response: {err}") from None
    raise AssertionError("unreachable")


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

    validation = cell(1, ROLE_COLUMN).get("dataValidation", {})
    condition = validation.get("condition", {})
    columns = grid.get("columnMetadata", [])
    return {
        "sheet_id": props["sheetId"],
        "row_count": props.get("gridProperties", {}).get("rowCount", 0),
        "frozen": props.get("gridProperties", {}).get("frozenRowCount", 0) >= 1,
        "id_hidden": bool(columns and columns[ID_COLUMN].get("hiddenByUser")),
        "notes_ok": all(cell(0, col).get("note") == WARNING_NOTE for col in (0, 1)),
        "dropdown_ok": (
            condition.get("type") == "ONE_OF_LIST"
            and [v.get("userEnteredValue") for v in condition.get("values", [])]
            == ROLE_LABELS
            and validation.get("strict") is True
        ),
        "marker_protections": [
            p["protectedRangeId"]
            for p in sheet.get("protectedRanges", [])
            if p.get("description") == PROTECTION_MARKER and p.get("warningOnly")
        ],
    }


def is_decorated(state: dict) -> bool:
    return (
        state["row_count"] >= 2
        and state["frozen"]
        and state["id_hidden"]
        and state["notes_ok"]
        and state["dropdown_ok"]
        and len(state["marker_protections"]) == 1
    )


def build_requests(state: dict) -> list[dict]:
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
    requests.append(
        {
            "setDataValidation": {
                "range": {
                    "sheetId": sid,
                    "startRowIndex": 1,  # open-ended: every data row
                    "startColumnIndex": ROLE_COLUMN,
                    "endColumnIndex": ROLE_COLUMN + 1,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": lbl} for lbl in ROLE_LABELS],
                    },
                    "strict": True,
                    "showCustomUi": True,
                },
            }
        }
    )
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


def decorate_one(token: str, file_id: str, dry_run: bool) -> str:
    spreadsheet = api_call(
        token, f"{SHEETS_API}/{file_id}?ranges=A1:H2&fields={GET_FIELDS}"
    )
    state = first_sheet_state(spreadsheet)
    if is_decorated(state):
        return "ok"
    if dry_run:
        return f"would decorate ({len(build_requests(state))} requests)"
    api_call(
        token,
        f"{SHEETS_API}/{file_id}:batchUpdate",
        {"requests": build_requests(state)},
    )
    return "decorated"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rclone-conf", required=True)
    parser.add_argument("--remote", required=True)
    parser.add_argument("--folder", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    try:
        token = mint_token(*oauth_from_conf(args.rclone_conf, args.remote))
        sheets = list_spreadsheets(args.rclone_conf, args.remote, args.folder)
    except (ServiceDisabled, SystemExit) as err:
        print(f"decorate: {err}", file=sys.stderr)
        return 2
    except Exception as err:  # token mint / rclone failure: nothing decoratable
        print(f"decorate: cannot start: {err}", file=sys.stderr)
        return 2

    failures = 0
    counts = {"ok": 0, "decorated": 0}
    for name, file_id in sheets:
        try:
            outcome = decorate_one(token, file_id, args.dry_run)
            counts[outcome] = counts.get(outcome, 0) + 1
            if args.verbose or outcome != "ok":
                print(f"{name}: {outcome}")
        except ServiceDisabled as err:
            print(f"decorate: {err}", file=sys.stderr)
            return 2
        except Exception as err:
            failures += 1
            print(f"decorate FAILED {name}: {err}", file=sys.stderr)
        time.sleep(1.1)  # the read quota is 60/min/user; 1.1s keeps 67 GETs under it
    print(
        f"decorate: {counts.get('decorated', 0)} decorated, "
        f"{counts.get('ok', 0)} already ok, {failures} failed of {len(sheets)}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
