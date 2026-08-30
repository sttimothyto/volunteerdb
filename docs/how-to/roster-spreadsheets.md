# Sync team rosters with Google Sheets

Each active team has a roster spreadsheet on Google Drive, shared **"anyone
with the link can edit"**. Ministry leaders and seconds hold the link, edit
the sheet in the browser, and every night the sheet and the database
reconcile **both ways**:

1. **02:30, sheet → database**: rows added or changed in the sheet are
   applied — new people join the roster, contact and role changes update.
2. **database → sheet**: the sheet is then rewritten from the resulting
   database state, so edits made in the app survive and the sheet matches
   the database by morning.

The sheet's columns are exactly the
[roster CSV format](../reference/spreadsheets.md); the Team column may be
left blank in a team's own sheet.

Everything to do with a team's spreadsheet — the link, the template, an
on-demand sync, and a `.csv` upload — lives in the **Roster spreadsheet**
section of that team's page, visible to its leaders, seconds and admins.

## A sync never removes anybody

This is the rule the whole feature rests on. Deleting a row from the sheet
does **not** end a membership: the write-back leg simply puts that person
back into the sheet the same night.

A spreadsheet is a surface people edit by hand, with filters, sorted views
and pasted blocks. Treating a missing row as a resignation would make every
clumsy edit a data-loss event. The importer has no removal path at all now —
not a setting left switched off, but code that is gone, along with the safety
thresholds (refuse an empty sheet, refuse to drop more than half a team) that
were only ever a way of noticing the accident after the fact. Take a member
off the roster **in the app**, where the change is attributable, reversible,
and visible in the as-of views.

## What leaders need to know

- **Keep the link private.** Anyone who has it can edit the sheet, which
  holds every member's email, phone and notes — and those edits reach the
  parish database on the next sync. Share it with the people who help run
  the team, and nobody else. There is no per-person access list any more;
  the link *is* the access.
- **Leave the ID column alone.** It pins each row to its database record, so
  correcting an email next to an ID updates that person instead of
  duplicating them. Rows for genuinely new people get a blank ID; the sync
  fills it in. (The column is hidden in a decorated sheet.)
- Blank-ID rows are matched email-first, exactly as manual imports: a new
  address on a blank-ID row creates a duplicate, and the sync records the
  same warnings the upload report shows.
- A sheet with problems (unknown team, unknown role, malformed ID) is
  **skipped whole**: nothing applies, the sheet is left untouched so the
  edits can be fixed, and the team page shows the error.
- Mistakes are recoverable: the 02:00 backup immediately precedes the sync
  ([backups](backup-restore.md)), and the history views show exactly what the
  sync changed (`changed_by` = `drive-sync@` your mail domain).
- **Only the first tab syncs.** The rewrite replaces the whole first sheet
  and can delete extra tabs — scratch work belongs in a copy (*File → Make a
  copy*; copies are never synced). The sheets carry a note saying so.

## Link a spreadsheet

A team without a sheet gets one made for it at the next nightly run, already
shared and decorated. To use your own instead, on the team page:

1. **Roster template (Google Sheets)** → *File → Make a copy*. The copy keeps
   the decoration (role dropdown, hidden ID column, structure warning).
2. In the copy, *Share* → **anyone with the link** → **Editor**. Viewer is
   not enough: the write-back leg cannot rewrite a sheet it may only read.
3. Back on the team page, **Link a spreadsheet** (or **Change spreadsheet**),
   paste the link, and choose which side wins the first sync:
   - *Overwrite it from the database* — rewrites the sheet from the roster.
     The answer that cannot lose parish data, and the right one for a fresh
     copy of the template.
   - *Import its rows into the database* — adds and updates from the sheet.
4. **Save** syncs straight away, so you find out at once whether the sheet is
   shared and shaped correctly.

A spreadsheet syncs with exactly one team; linking one that already belongs
to another team is refused.

**Sync now** re-runs the sheet → database → sheet cycle on demand.
**Overwrite sheet** rewrites the sheet from the database, discarding whatever
is in it — the way out of a mangled sheet.

## Import a `.csv`

Under the same heading, the upload accepts one roster `.csv`:

1. Prepare the file — an export is the best starting point for bulk edits,
   the template for new data. Keep the **ID** column as exported and leave it
   blank on rows for new people. A row with a blank **Team** just adds or
   updates the person; a row with a **Team** and **Role** also puts them on
   that team.
2. Upload it. The app **always dry-runs first**: nothing is written, and you
   get a row-by-row report of what would be created and updated.
3. Read the report. Any error (unknown team, ambiguous volunteer, invalid
   role, unknown ID, …) blocks the whole file — imports are all-or-nothing.
   For leaders and seconds, rows outside their teams are errors too, and a
   new volunteer needs a Team on one of their teams somewhere in the file.
4. When the report is clean, click **Apply this import**.

Imports only **add and update**; a blank cell never clears a field. One
restore caveat: a historical `as_of=` export may carry IDs of volunteers
deleted since, and those rows are errors on re-import — blank the ID cells to
recreate the people instead. Photos are managed in the app and API only.

## Export

- **Export team(s)** on `/teams` — the whole parish for admins; for everyone
  else, every team they lead or sit on the core of, in one file. Hidden
  entirely from plain members.
- **Export roster (.csv)** on a team page — that one team, for anyone with
  full-roster rights on it.
- **Roster template** — the decorated template sheet, or a bare header-row
  CSV in development where `VDB_TEMPLATE_SHEET_URL` is unset.

Via the API, the data exports accept `as_of=` for historical snapshots — see
the {ref}`endpoints <api-import-export>`.

## Sheet decoration (self-healing)

Every sheet gets leader-facing polish: a **Role dropdown** of the four role
labels (strict — a free-typed role is rejected at entry instead of failing
the night's sync), a **Team dropdown** holding exactly that sheet's own team
path, a **structure warning** note on the first two header cells, a
**frozen, warning-protected header row**, and a **hidden ID column** (hidden
columns still export, so the pin survives).

The write-back wipes cell metadata, so re-decoration is a leg of the sync
itself (`services/gsheets.py:decorate`), skipped for sheets already
compliant so Drive version history stays quiet. Every decoration request is
field-masked so it cannot reach cell values.

## Architecture

The two legs are deliberately asymmetric, and the asymmetry is what makes
leader-owned sheets possible at all:

- **Read** — an anonymous `GET` of
  `https://docs.google.com/spreadsheets/d/<id>/export?format=csv`. No token,
  no quota, and it works on any sheet shared by link, wherever it lives. This
  is the same endpoint `services/pages.py` uses to fetch a published Google
  Doc.
- **Write** — Sheets v4 under the parish account's OAuth token. "Anyone with
  the link can edit" is what puts a leader's own file in that token's reach.

Both run inside the app (`services/gsheets.py`), driven by the in-app
scheduler's `roster_sync` job. There is no host-side leg: the rclone
transport, its work dir, `renames.txt`, `manifest.json` and the systemd timer
were all scaffolding around a `drive.file` grant that could only see files
this system created, and they were removed with it. The nightly *backup* to
Drive is unrelated and still uses rclone.

A task-force team never gets a sheet: it holds a borrowed roster, and syncing
one would publish the collaborating teams' contact details into a
link-shared file.

## Configure it

The read leg needs nothing. The write leg needs a one-time authorization on a
machine with a browser, signed in as the parish Google account:

```sh
python scripts/google_authorize.py CLIENT_ID CLIENT_SECRET
```

It prints `VDB_SHEETS_REFRESH_TOKEN`. Set that alongside
`VDB_SHEETS_CLIENT_ID`, `VDB_SHEETS_CLIENT_SECRET` and
`VDB_SHEETS_FOLDER_ID` (the Drive folder id new sheets are created in) — see
[configuration](../reference/configuration.md). The deploy reads them back on
later runs, so they are set once.

An instance whose sheets were created by the retired rclone sync has them
shared per leader rather than by link, and until that changes the read leg
gets a sign-in page instead of a roster ("the spreadsheet is not shared").
Run `scripts/share_roster_sheets.py` once — after the settings are in place
and before the first nightly run, `--dry-run` first — to share every sheet in
`team_sheet` by link ([Commands and scripts](../reference/cli.md)). A fresh
instance never needs it.

The scopes the sheets use are `spreadsheets` (read and write any sheet the
account can reach) and `drive.file` (create a sheet in the folder and set
its sharing, limited to files this client created). Deliberately **not**
full `drive`: nothing here enumerates or deletes the account's other files.
The same token also carries the two calendar scopes the
[parish calendar](google-calendar-sync.md) needs — one authorization serves
both, which is why the script asks for all four at once.

With the settings empty the job exits "not configured" and the team page says
so — which is the normal state in development.

## Scripted use

```sh
curl -s -H "Authorization: Bearer $TOKEN" -o parish.csv \
  https://vdb.example.org/api/export/parish.csv
curl -s -H "Authorization: Bearer $TOKEN" -o my-teams.csv \
  https://vdb.example.org/api/export/my-teams.csv
curl -s -H "Authorization: Bearer $TOKEN" -F file=@parish.csv \
  'https://vdb.example.org/api/import?dry_run=true'
```

Drop `dry_run=true` to apply. To link and sync a sheet:

```sh
curl -s -X PATCH -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"url":"https://docs.google.com/spreadsheets/d/FILE_ID/edit"}' \
  https://vdb.example.org/api/teams/7/roster-sheet
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"direction":"import"}' \
  https://vdb.example.org/api/teams/7/roster-sheet/sync
```

See [Use the JSON API](api-recipes.md) for obtaining `$TOKEN`.
