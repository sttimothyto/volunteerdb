# Sync team rosters with Google Sheets

Each active team has a native Google Sheet in the Drive folder
**`volunteerdb-spreadsheets`** (account `admin@sttimothyto.org`), named
`<team-path-slug>-membership-list` — e.g. `liturgy-music-membership-list`
for *Liturgy / Music*. Ministry leaders and seconds edit their sheet in the
browser; every night the sheet and the database reconcile **both ways**:

1. **02:30, sheet → database**: edits made in a sheet since the last sync
   are applied — new rows join the roster, role/contact changes update, and
   **a deleted row ends that membership** (history keeps it; a volunteer
   losing their last membership anywhere is archived).
2. **database → sheet**: each sheet is then rewritten from the resulting
   database state, so UI edits survive and the sheet always matches the
   database by morning.

The sheet's columns are exactly the
[roster CSV format](../reference/spreadsheets.md); the Team column may be
left blank in a team's own sheet.

## What leaders need to know

- The team page in VolunteerDB shows the **Google Sheet** link (and the
  last sync status) to the team's leaders/seconds. Edit access to the sheet
  is granted **on the Google side** — share the sheet or the folder from
  `admin@sttimothyto.org`.
- Deleting a row removes that person from the roster at the next sync.
  Their history is preserved and visible in as-of views and timelines.
  Re-adding a row for someone the sync had archived puts them back on the
  team **and reactivates them** — joining implies active.
- **Leave the ID column alone.** It pins each row to its database record;
  correcting an email next to an ID updates the person instead of
  duplicating them. Rows for genuinely new people get a blank ID; the sync
  fills it in overnight.
- Blank-ID rows are matched email-first, exactly as manual imports: a new
  address on a blank-ID row creates a duplicate (the sync records the same
  warnings the import page shows).
- A sheet with problems (unknown role, unknown ID, an **empty sheet for a
  team that has members**, or a wipe of more than half the team and ≥ 3
  members — the safety thresholds) is **skipped whole**: nothing applies,
  the sheet is left untouched for fixing, the team page shows the error,
  and `admin@sttimothyto.org` gets an email.
- The alert email can also arrive for a sheet that **did** apply: when one
  sync both created a volunteer and removed a member (the signature of an
  edited email on a blank-ID row, or a row deleted and re-typed), a
  `WARNING` line asks a human to check for a duplicated person.
- Mistakes are recoverable: the 02:00 backup immediately precedes the
  sync ([backups](backup-restore.md)); the history views show exactly what
  the sync changed (`changed_by` = `drive-sync@sttimothyto.org`).
- **Only the first tab syncs.** The nightly rewrite replaces the whole
  spreadsheet and can **delete any extra tabs** — scratch work belongs in
  a copy (*File → Make a copy*; copies are never synced), not in extra
  tabs. The sheets carry a note saying the same.

## Sheet decoration (self-healing)

Every sheet gets leader-facing polish: a **Role dropdown** of the four
role labels (strict — free-typed roles are rejected at entry instead of
failing the night's sync), a **structure warning** note on the first two
header cells, a **frozen, warning-protected header row**, and a **hidden
ID column** (hidden columns still export, so the pin survives).

The nightly rewrite wipes all of this — decoration is therefore a leg of
the sync itself (`/usr/local/bin/volunteerdb-decorate-sheets`, installed
from `deploy/files/volunteerdb-decorate-sheets.py`): after the upload
loop it re-decorates every roster sheet and the template, skipping sheets
already compliant. It runs **before** the relist that `record` reads —
decoration bumps Drive ModTime, and the stored sync marks must postdate
it or every sheet would look leader-edited the next night. A decoration
failure emails an alert but never fails the data sync. Never run it by
hand between a sync's `record` and the next timer run for the same reason;
if you must, immediately re-run the full sync afterwards.

It authenticates by minting an access token from the rclone remote's
OAuth client (`rclone.conf` is read, never written) and needs the
**Google Sheets API enabled** in that client's Cloud project — a
`SERVICE_DISABLED` error aborts with the one-time activation URL.

## The template sheet

The Drive folder also holds **`ROSTER TEMPLATE (copy me, do not edit)`**
— a decorated, header-only sheet the `/import` page links to
(`VDB_TEMPLATE_SHEET_URL` in `/etc/volunteerdb/env`; it replaced the old
`GET /api/export/template.csv`). Its name matches no team slug, so the
sync leaves it alone (a nightly journal `NOTE` line is expected). To
(re)create it:

```sh
CONF=/root/.config/rclone/rclone.conf
printf '\xef\xbb\xbfID,First name,Last name,Email,Phone,Volunteer notes,Team,Role\r\n' > /tmp/template.csv
rclone --config $CONF --drive-import-formats csv copyto /tmp/template.csv \
  "volunteerdb-gdrive-backup:volunteerdb-spreadsheets/ROSTER TEMPLATE (copy me, do not edit).csv"
```

The next sync (or a standalone decorate run right after, if no sync has
run since) decorates it, adding 25 dropdown-ready blank rows.

## Architecture

Everything Google-facing runs **on the host via rclone** (the same
`volunteerdb-gdrive-backup` remote the backups use — its `drive.file`
scope sees exactly the files this job creates, and the sync uses the plain
remote, *not* the crypt wrapper, because these files must be
human-editable). The Python side never talks to Drive: the host script
`/usr/local/bin/volunteerdb-drive-sync` (template
`deploy/templates/volunteerdb-drive-sync.sh.j2`) exports every sheet to CSV
in a work dir (`/var/lib/volunteerdb-drive-sync`), runs
`python -m volunteerdb.jobs.drive_sync apply /sync` in a one-shot app
container, uploads the regenerated CSVs back with convert-on-upload,
re-decorates the sheets (see above), then `… record /sync` stores each
sheet's Drive **file id** and sync mark.

Details that make it safe:

- **File ids, not names, are identity**: a team rename shows up as a
  rename of the Drive file (`renames.txt` → `rclone moveto`), so the
  leader's bookmarked link keeps working.
- **Staleness check**: a sheet whose Drive ModTime hasn't advanced past
  `team_sheet.last_synced_at` skips the apply leg — the database wins
  unless a human actually edited the sheet. Conflicts need both sides
  edited the same day; the sheet's version then applies and the UI edit is
  visible in history.
- **Unchanged sheets are not re-uploaded** (parsed-CSV comparison), so
  Drive version history doesn't churn nightly.
- **Failed sheets are never overwritten** — the leader's edits stay on
  Drive to be corrected.
- Sheets on Drive matching no active team (team archived/deleted) are left
  alone and logged.

Logs: `journalctl -u volunteerdb-drive-sync`. Manual run:
`ssh sttimothyto-prod 'systemctl start volunteerdb-drive-sync.service'`.

## One-time verification runbook (BEFORE trusting the nightly sync)

The load-bearing assumption is that rclone **updates a converted Google
Sheet in place** — same file id — when re-uploading its CSV. If the id
churned, every shared link would break nightly. Verify once on the prod
host against a scratch folder:

```sh
CONF=/root/.config/rclone/rclone.conf
R="rclone --config $CONF --drive-export-formats csv --drive-import-formats csv"
REMOTE=volunteerdb-gdrive-backup:vdb-sync-test

printf 'First name,Last name\nTest,Person\n' > /tmp/t.csv
$R mkdir $REMOTE
$R copyto /tmp/t.csv $REMOTE/t.csv          # creates a native Google Sheet
$R lsjson $REMOTE                            # note "ID" of t
# open the sheet in the browser (as admin@), edit a cell, then:
$R copyto /tmp/t.csv $REMOTE/t.csv           # re-upload
$R lsjson $REMOTE                            # ID must be UNCHANGED
$R moveto $REMOTE/t.csv $REMOTE/t2.csv       # rename must keep the ID too
$R lsjson $REMOTE
$R copy $REMOTE /tmp/vdb-sync-test/          # export leg round-trips as CSV
$R purge $REMOTE                             # clean up
```

Confirm: (a) the ID survives `copyto` re-upload, (b) `moveto` renames the
sheet keeping the ID, (c) the export leg produces readable CSV, (d)
`lsjson` reports IDs at all under the `drive.file` scope. **Fallback if the
ID churns on re-upload**: keep rclone for the export leg and do the upload
leg with the Drive API `files.update` endpoint (httpx, OAuth token read
from `rclone.conf`) in a small `jobs/drive_upload.py` — no schema change
needed, `record` reconciles by name.

Then: run the sync once by hand (command above), check the sheets appear
in Drive, share the folder with the leaders, and open a team page to see
the link.

## Recovery

- **A sync applied something wrong**: inspect with the as-of picker
  (changes are attributed to `drive-sync@sttimothyto.org`); fix forward in
  the app, or restore last night's 02:00 backup
  ([backup & restore](backup-restore.md)) if it was catastrophic.
- **A sheet is broken beyond repair**: delete the file on Drive; the next
  sync bootstraps a fresh one from the database (the team page link
  updates to the new file id automatically).
