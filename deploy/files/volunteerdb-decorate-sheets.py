#!/usr/bin/env python3
"""Re-apply leader-facing decoration, and per-leader edit access, to every
roster sheet on Drive.

Convert-on-upload (the sync's rclone leg) replaces a spreadsheet's whole
body, wiping validation, notes, hidden columns and protections — so this
runs as a nightly self-healing leg of volunteerdb-drive-sync, after the
upload loop and BEFORE the relist that `record` reads: decoration bumps
Drive ModTime, and the stored sync marks must postdate it or every sheet
looks leader-edited the next night.

Per sheet (first tab): Role and Team column dropdowns (strict), a
structure warning as a note on A1+B1, frozen header row, hidden ID
column, and a warning-only protection on the header. All requests are
metadata/format-only by field-mask construction — they cannot touch cell
values. Sheets already compliant are skipped so Drive version history
stays quiet.

Sharing (--manifest): the team's leaders and seconds are granted writer
on their own sheet, and nobody is ever removed except an anyone-with-link
or domain-wide grant, and only under --revoke-links. The owner, the
folder-inherited grants and hand-added people are left alone. Sharing
failures are reported, never fatal — a sheet is data, not access.

The Team dropdown values and the editor lists both come from the app,
via the manifest.json that `drive_sync apply` leaves in the work dir:
this script knows Drive, the app knows the roster, and neither imports
the other.

Runs on the HOST (stdlib only): rclone lists the folder; auth reuses the
rclone remote's OAuth client by minting an access token from its refresh
token (rclone.conf is never written). Requires the Google Sheets API to
be enabled for that client's Cloud project — a SERVICE_DISABLED abort
prints the activation URL. The Drive API is already enabled there (rclone
itself uses it), and its drive.file scope covers permission writes on the
files this system created.
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
TEAM_COLUMN = 6  # 0-based G; ROSTER_HEADERS.index("Team")
ID_COLUMN = 0  # column A; ROSTER_HEADERS.index("ID")
PROTECTION_MARKER = "volunteerdb-sync"
# Sheets caps a ONE_OF_LIST rule; past it, skip the Team rule rather than
# fail the sheet (the parish would need 500 teams to get here).
MAX_VALIDATION_VALUES = 500
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
DRIVE_API = "https://www.googleapis.com/drive/v3/files"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PERMISSION_FIELDS = "permissions(id,type,role,emailAddress,permissionDetails)"
# Sent once, when a leader first gains access: Drive refuses a writer grant to
# an address with no Google account unless it may notify, and for a leader on
# hotmail/yahoo this mail is the only way they learn the sheet exists.
SHARE_MESSAGE = (
    "You lead this ministry in VolunteerDB, so you now have edit access to "
    "its roster spreadsheet. What you change here is applied to the parish "
    "database overnight, and the sheet is rewritten to match."
)
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


def api_call(
    token: str, url: str, payload: dict | None = None, method: str | None = None
) -> dict:
    """GET (payload None), POST, or an explicit method, with retry on 429/5xx;
    SERVICE_DISABLED raises immediately — every subsequent sheet would fail the
    same way. A DELETE answers 204 with no body, hence the empty-body case."""
    for attempt in range(3):
        req = urllib.request.Request(
            url,
            method=method,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read()
                return json.loads(body) if body else {}
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
        "dropdown_ok": _dropdown_values(cell(1, ROLE_COLUMN)) == ROLE_LABELS,
        "team_values": _dropdown_values(cell(1, TEAM_COLUMN)),
        "marker_protections": [
            p["protectedRangeId"]
            for p in sheet.get("protectedRanges", [])
            if p.get("description") == PROTECTION_MARKER and p.get("warningOnly")
        ],
    }


def is_decorated(state: dict, team_values: list[str] | None = None) -> bool:
    """team_values None means the caller could not resolve a Team dropdown for
    this sheet (no manifest, or a sheet matching no active team); whatever the
    sheet already carries in that column is then left as it is."""
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
    requests.append(_validation_request(sid, ROLE_COLUMN, ROLE_LABELS))
    if team_values is not None:
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


def decorate_one(
    token: str, file_id: str, dry_run: bool, team_values: list[str] | None = None
) -> str:
    spreadsheet = api_call(
        token, f"{SHEETS_API}/{file_id}?ranges=A1:H2&fields={GET_FIELDS}"
    )
    state = first_sheet_state(spreadsheet)
    if is_decorated(state, team_values):
        return "ok"
    if dry_run:
        return f"would decorate ({len(build_requests(state, team_values))} requests)"
    api_call(
        token,
        f"{SHEETS_API}/{file_id}:batchUpdate",
        {"requests": build_requests(state, team_values)},
    )
    return "decorated"


def load_manifest(path: str | None) -> dict | None:
    """The app's half of the picture, written by `drive_sync apply`:
    {"teams": [display path, ...], "sheets": {drive name: {team, editors}}}.

    No --manifest at all means decorate-only: no Team dropdown, no sharing.
    A manifest that is there but unreadable is a hard stop instead, because
    falling through would skip a night's sharing with a zero exit status —
    and neither half of this may ever be guessed at, since guessing wrong
    hands someone the wrong roster.
    """
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError) as err:
        raise SystemExit(f"cannot read manifest {path!r}: {err}") from None
    if not isinstance(manifest.get("sheets"), dict):
        raise SystemExit(f"manifest {path!r} has no sheets map")
    return manifest


def team_values_for(name: str, manifest: dict | None) -> list[str] | None:
    """Dropdown values for one sheet's Team column, or None to leave it alone.

    A team's own sheet offers exactly its own path: the sync rejects a row
    naming any other team outright, so anything else on that list is a trap.
    The template belongs to no team and is copied for hand imports, so it
    offers all of them.
    """
    if manifest is None:
        return None
    values = manifest["teams"] if "TEMPLATE" in name else None
    if values is None:
        entry = manifest["sheets"].get(name)
        values = [entry["team"]] if entry else None
    if values is not None and len(values) > MAX_VALIDATION_VALUES:
        print(
            f"{name}: {len(values)} teams exceeds the dropdown cap — Team rule skipped"
        )
        return None
    return values


def _inherited(permission: dict) -> bool:
    """Granted on the parent folder, not this file. Drive refuses to delete
    such a permission from the file — it has to go from the folder."""
    return any(d.get("inherited") for d in permission.get("permissionDetails", []))


def share_plan(
    permissions: list[dict], editors: list[str], revoke_links: bool
) -> tuple[list[str], list[str], list[str]]:
    """(emails to grant writer, permission ids to delete, ids blocked) for one
    sheet.

    Conservative by construction: the only thing this ever removes is an
    anyone-with-link or domain-wide grant, and only when asked to. The owner,
    folder-inherited grants, and anyone a human shared the sheet with by hand
    are never touched — this reconciles access, it does not police it.
    """
    present = set()
    remove: list[str] = []
    blocked: list[str] = []
    for permission in permissions:
        if permission.get("type") == "user":
            email = (permission.get("emailAddress") or "").strip().lower()
            if email:
                present.add(email)
        elif permission.get("type") in ("anyone", "domain") and revoke_links:
            target = blocked if _inherited(permission) else remove
            target.append(permission["id"])
    add: list[str] = []
    for editor in editors:
        # the manifest is already normalised, but two leaders sharing a family
        # address must still not be invited twice in one pass
        email = (editor or "").strip().lower()
        if email and email not in present:
            present.add(email)
            add.append(email)
    return add, remove, blocked


def share_one(
    token: str, file_id: str, editors: list[str], revoke_links: bool, dry_run: bool
) -> dict:
    """Reconcile one sheet's access. Never raises for a single bad address:
    a leader whose email Drive refuses must not cost the other 60 sheets
    their sync."""
    permissions = api_call(
        token, f"{DRIVE_API}/{file_id}/permissions?fields={PERMISSION_FIELDS}"
    )["permissions"]
    add, remove, blocked = share_plan(permissions, editors, revoke_links)
    result = {"granted": 0, "revoked": 0, "blocked": len(blocked), "errors": []}
    query = urllib.parse.urlencode(
        {"sendNotificationEmail": "true", "emailMessage": SHARE_MESSAGE, "fields": "id"}
    )
    for email in add:
        if dry_run:
            result["granted"] += 1
            continue
        try:
            api_call(
                token,
                f"{DRIVE_API}/{file_id}/permissions?{query}",
                {"type": "user", "role": "writer", "emailAddress": email},
                method="POST",
            )
        except Exception as err:  # a rejected address, not a broken run
            result["errors"].append(f"cannot grant {email}: {err}")
        else:
            result["granted"] += 1
    for permission_id in remove:
        if dry_run:
            result["revoked"] += 1
            continue
        try:
            api_call(
                token,
                f"{DRIVE_API}/{file_id}/permissions/{permission_id}",
                method="DELETE",
            )
        except Exception as err:
            result["errors"].append(f"cannot revoke {permission_id}: {err}")
        else:
            result["revoked"] += 1
    return result


def write_alerts(path: str | None, lines: list[str]) -> None:
    """Append to the sync's alerts file; a non-empty file is what makes the
    nightly mail go out. Failing to write an alert must not fail the run."""
    if not path or not lines:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.writelines(f"{line}\n" for line in lines)
    except OSError as err:
        print(f"cannot write alerts to {path!r}: {err}", file=sys.stderr)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rclone-conf", required=True)
    parser.add_argument("--remote", required=True)
    parser.add_argument("--folder", required=True)
    parser.add_argument(
        "--manifest", help="drive_sync apply's manifest.json; enables Team + sharing"
    )
    parser.add_argument(
        "--revoke-links",
        action="store_true",
        help="also remove anyone-with-link and domain-wide grants",
    )
    parser.add_argument("--alerts-file", help="append problems here for the sync mail")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    try:
        manifest = load_manifest(args.manifest)
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
    share = {"granted": 0, "revoked": 0, "blocked": 0}
    unshared: list[str] = []
    alerts: list[str] = []
    for name, file_id in sheets:
        try:
            outcome = decorate_one(
                token, file_id, args.dry_run, team_values_for(name, manifest)
            )
            # a dry run's outcome carries its request count, so bucket on the
            # verdict and keep the detail for the per-sheet line
            counts["ok" if outcome == "ok" else "decorated"] += 1
            if args.verbose or outcome != "ok":
                print(f"{name}: {outcome}")
        except ServiceDisabled as err:
            print(f"decorate: {err}", file=sys.stderr)
            return 2
        except Exception as err:
            failures += 1
            print(f"decorate FAILED {name}: {err}", file=sys.stderr)

        entry = manifest["sheets"].get(name) if manifest else None
        if entry is not None:
            try:
                result = share_one(
                    token, file_id, entry["editors"], args.revoke_links, args.dry_run
                )
            except ServiceDisabled as err:  # every other sheet would fail alike
                print(f"share: {err}", file=sys.stderr)
                return 2
            except Exception as err:
                failures += 1
                alerts.append(f"sheet {name!r}: cannot read or set access: {err}")
                print(f"share FAILED {name}: {err}", file=sys.stderr)
            else:
                for key in share:
                    share[key] += result[key]
                for message in result["errors"]:
                    alerts.append(f"sheet {name!r}: {message}")
                    print(f"share {name}: {message}", file=sys.stderr)
                if not entry["editors"]:
                    unshared.append(entry["team"])
                if args.verbose or result["granted"] or result["revoked"]:
                    print(
                        f"{name}: +{result['granted']} editor(s), "
                        f"-{result['revoked']} link/domain grant(s)"
                    )
        time.sleep(1.1)  # the read quota is 60/min/user; 1.1s keeps 67 GETs under it

    if unshared:
        # Not a failure: a team with no leader on file is a roster gap, and the
        # nightly mail is the only place anyone would notice it.
        alerts.append(
            f"{len(unshared)} team(s) have no leader or second with an email, so "
            "nobody but the parish account can edit their sheet: "
            + ", ".join(sorted(unshared))
        )
    if share["blocked"]:
        alerts.append(
            f"{share['blocked']} link/domain grant(s) are inherited from the Drive "
            "folder and cannot be removed per sheet — unshare the folder itself"
        )
    if not args.dry_run:
        write_alerts(args.alerts_file, alerts)
    print(
        f"decorate: {counts.get('decorated', 0)} decorated, "
        f"{counts.get('ok', 0)} already ok, {failures} failed of {len(sheets)} | "
        f"share: {share['granted']} granted, {share['revoked']} revoked, "
        f"{len(unshared)} with no editor"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
