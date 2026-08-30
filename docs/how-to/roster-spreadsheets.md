# Sync team rosters with Google Sheets

- Each active team has a roster spreadsheet on Google Drive, shared
  **"anyone with the link can edit"**.
- Ministry leaders and seconds hold the link and edit the sheet in the
  browser.
- Every night the sheet and the database reconcile **both ways**:

1. **02:30, sheet → database**: the sync applies rows added or changed in
   the sheet. New people join the roster; contact and role changes update.
2. **database → sheet**: the sync then rewrites the sheet from the new
   database state. Edits made in the app survive, and the sheet matches the
   database by morning.

- The sheet's columns are exactly the
  [roster CSV format](../reference/spreadsheets.md). The Team column can
  stay blank in a team's own sheet.
- Everything to do with a team's spreadsheet lives in the
  *Roster spreadsheet* section of that team's page. That is the link, the
  template, an on-demand sync, and a `.csv` upload. Its leaders, seconds and
  admins see that section.

## A sync never removes anybody

- This is the rule the whole feature rests on.
- A deleted row in the sheet does **not** end a membership. The write-back
  leg puts that person back into the sheet the same night.
- A spreadsheet is a surface people edit by hand, with filters, sorted views
  and pasted blocks. If a lost row meant a resignation, every clumsy edit
  would be a data-loss event.
- The importer has no removal path at all now. That is not a setting left
  switched off; the code is gone. The safety thresholds went with it (refuse
  an empty sheet, refuse to drop more than half a team). They were only ever
  a way to notice the accident after the fact.
- Take a member off the roster **in the app**. There the change is
  attributable, reversible, and visible in the as-of views.

## What leaders need to know

- **Keep the link private.** Anyone who has it can edit the sheet, which
  holds every member's email, phone and notes. Those edits reach the parish
  database on the next sync. Share it with the people who help run the team,
  and nobody else. There is no per-person access list any more; the link
  *is* the access.
- **Leave the ID column alone.** It pins each row to its database record. So
  a corrected email next to an ID updates that person instead of a
  duplicate. Rows for genuinely new people get a blank ID; the sync fills it
  in. (The column is hidden in a decorated sheet.)
- The sync matches blank-ID rows email-first, exactly as manual imports do.
  A new address on a blank-ID row creates a duplicate, and the sync records
  the same warnings the upload report shows.
- The sync **skips a sheet with problems whole** (unknown team, unknown
  role, malformed ID). Nothing applies, the sheet stays untouched so you can
  fix the edits, and the team page shows the error.
- Mistakes are recoverable. The 02:00 backup immediately precedes the sync
  ([backups](backup-restore.md)). The history views show exactly what the
  sync changed (`changed_by` = `drive-sync@` your mail domain).
- **Only the first tab syncs.** The rewrite replaces the whole first sheet
  and can delete extra tabs. Scratch work belongs in a copy
  (*File → Make a copy*; the sync never touches copies). The sheets carry a
  note that says so.

## Link a spreadsheet

- A team without a sheet gets one made for it at the next nightly run,
  already shared and decorated.
- To use your own instead, on the team page:

1. Click *Roster template (Google Sheets)*.
2. In the template, *File → Make a copy*. The copy keeps the decoration
   (role dropdown, hidden ID column, structure warning).
3. In the copy, *Share* → **anyone with the link** → **Editor**. Viewer is
   not enough: the write-back leg cannot rewrite a sheet it can only read.
4. Back on the team page, click *Link a spreadsheet* (or
   *Change spreadsheet*).
5. Paste the link.
6. Choose which side wins the first sync:
   - *Overwrite it from the database* — rewrites the sheet from the roster.
     This answer cannot lose parish data, and it is the right one for a
     fresh copy of the template.
   - *Import its rows into the database* — adds and updates from the sheet.
7. Click *Save*. It syncs straight away, so you find out at once whether the
   sheet is shared and shaped correctly.

- A spreadsheet syncs with exactly one team. The app refuses a link to a
  sheet that already belongs to another team.
- *Sync now* re-runs the sheet → database → sheet cycle on demand.
- *Overwrite sheet* rewrites the sheet from the database and discards
  whatever is in it. It is the way out of a mangled sheet.

## Import a `.csv`

Under the same heading, the upload accepts one roster `.csv`:

1. Prepare the file. An export is the best start for bulk edits; the
   template is best for new data.
   - Keep the **ID** column as exported. Leave it blank on rows for new
     people.
   - A row with a blank **Team** only adds or updates the person.
   - A row with a **Team** and a **Role** also puts them on that team.
2. Upload it. The app **always dry-runs first**. It writes nothing, and you
   get a row-by-row report of what it would create and update.
3. Read the report. Any error blocks the whole file. Examples: an unknown
   team, an ambiguous volunteer, an invalid role, an unknown ID. Imports are
   all-or-nothing.
   - For leaders and seconds, rows outside their teams are errors too.
   - A new volunteer needs a Team on one of their teams somewhere in the
     file.
4. When the report is clean, click *Apply this import*.

- Imports only **add and update**; a blank cell never clears a field.
- One restore caveat: a historical `as_of=` export can carry IDs of
  volunteers deleted since. Those rows are errors on re-import. Blank the ID
  cells to recreate the people instead.
- You manage photos in the app and the API only.

## Export

- *Export team(s)* on `/teams` — the whole parish for admins. For everyone
  else, every team they lead or sit on the core of, in one file. Plain
  members do not see it at all.
- *Export roster (.csv)* on a team page — that one team, for anyone with
  full-roster rights on it.
- **Roster template** — the decorated template sheet. In development, where
  `VDB_TEMPLATE_SHEET_URL` is unset, the button is *Empty template* and gives
  a bare header-row CSV.

Through the API, the data exports accept `as_of=` for historical snapshots.
See the {ref}`endpoints <api-import-export>`.

## Sheet decoration (self-healing)

Every sheet gets leader-facing polish:

- a **Role dropdown** of the four role labels. It is strict: the sheet
  rejects a free-typed role at entry, instead of a failure in the night's
  sync;
- a **Team dropdown** that holds exactly that sheet's own team path;
- a **structure warning** note on the first two header cells;
- a **frozen, warning-protected header row**;
- a **hidden ID column** (hidden columns still export, so the pin survives).

Notes:

- The write-back wipes cell metadata, so re-decoration is a leg of the sync
  itself (`services/gsheets.py:decorate`). The sync skips sheets that
  already comply, so Drive version history stays quiet.
- Every decoration request is field-masked, so it cannot reach cell values.

## Architecture

The two legs are asymmetric on purpose. The asymmetry is what makes
leader-owned sheets possible at all:

- **Read** — an anonymous `GET` of
  `https://docs.google.com/spreadsheets/d/<id>/export?format=csv`. No token,
  no quota. It works on any sheet shared by link, wherever it lives. This is
  the same endpoint `services/pages.py` uses to fetch a published Google
  Doc.
- **Write** — Sheets v4 under the parish account's OAuth token. "Anyone with
  the link can edit" is what puts a leader's own file in that token's reach.

Notes:

- Both legs run inside the app (`services/gsheets.py`). The in-app
  scheduler's `roster_sync` job drives them.
- There is no host-side leg. The rclone transport, its work dir,
  `renames.txt`, `manifest.json` and the systemd timer were all scaffolding
  around a `drive.file` grant. That grant could only see files this system
  created, and all of it went away with the grant.
- The nightly *backup* to Drive is unrelated and still uses rclone.
- A task-force team never gets a sheet. It holds a borrowed roster, and a
  sync would publish the contact details of the teams that collaborate into
  a link-shared file.

## Configure it

- The read leg needs nothing.
- The write leg needs a one-time authorization on a machine with a browser,
  signed in as the parish Google account:

```sh
python scripts/google_authorize.py CLIENT_ID CLIENT_SECRET
```

- It prints `VDB_SHEETS_REFRESH_TOKEN`.
- Set that alongside `VDB_SHEETS_CLIENT_ID`, `VDB_SHEETS_CLIENT_SECRET` and
  `VDB_SHEETS_FOLDER_ID` (the Drive folder id that new sheets go in). See
  [configuration](../reference/configuration.md).
- The deploy reads them back on later runs, so you set them once.

An instance that predates the current sync:

- An instance whose sheets came from the retired rclone sync has them shared
  per leader, not by link. Until that changes, the read leg gets a sign-in
  page instead of a roster ("the spreadsheet is not shared").
- Run `scripts/share_roster_sheets.py` once to share every sheet in
  `team_sheet` by link ([Commands and scripts](../reference/cli.md)). Run it
  after the settings are in place and before the first nightly run. Run it
  with `--dry-run` first.
- A fresh instance never needs it.

The scopes:

- The scopes the sheets use are `spreadsheets` (read and write any sheet the
  account can reach) and `drive.file`. The `drive.file` scope creates a
  sheet in the folder and sets its access, limited to files this client
  created.
- Not full `drive`, on purpose: nothing here enumerates or deletes the
  account's other files.
- The same token also carries the two calendar scopes the
  [parish calendar](google-calendar-sync.md) needs. One authorization serves
  both, which is why the script asks for all four at once.
- With the settings empty, the job exits "not configured" and the team page
  says so. That is the normal state in development.

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

See [Use the JSON API](api-recipes.md) for how to get `$TOKEN`.
