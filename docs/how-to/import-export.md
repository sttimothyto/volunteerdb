# Import and export spreadsheets

Bulk data moves through one roster `.csv` that round-trips: export, edit,
re-import. One row per person per team; column semantics, matching rules,
and limits are in the
[spreadsheet format reference](../reference/spreadsheets.md). The GUI lives
at **`/import`** (header → *Import/Export*) and is available to admins and
to team leaders/seconds; leader imports and exports cover only the teams
they lead (sub-teams included).

## Export

- **Full parish** — `/import` → *Full parish export*: every membership row,
  then volunteers with no membership, including custom-field columns.
  Admin only.
- **My teams** — for leaders/seconds, `/import` → *My teams export*: the
  union of their managed teams.
- **One team's roster** — the *Export roster (.csv)* button on a team page;
  available to anyone with full-roster rights on that team.
- **Empty template** — `/import` → *Empty template*: the header row, for
  building an import from scratch.

Via the API, the data exports accept `as_of=` for historical snapshots —
see the {ref}`endpoints <api-import-export>`.

## Import

1. Prepare the file — an export is the best starting point for bulk edits;
   the template for new data. Keep the **ID** column as exported (it pins
   each row to its record and makes email corrections safe) and leave it
   blank on rows for new people. A row with a blank **Team** just adds or
   updates the person; a row with a **Team** and **Role** also puts them on
   that team.
2. On `/import`, upload the file. The app **always dry-runs first**: nothing
   is written, and you get a row-by-row report of what would be created,
   updated, and any problems.
3. Read the report. Any error (unknown team, ambiguous volunteer, invalid
   role, unknown ID, …) blocks the whole file — imports are all-or-nothing,
   so fix the spreadsheet and re-upload rather than hoping for partial
   application. For leaders/seconds, rows outside their teams are errors
   too, and a new volunteer needs a Team on one of their teams in some row
   of the same file.
4. When the report is clean, click **Apply this import**.

One restore caveat: a historical `as_of=` export may carry IDs of
volunteers deleted since. Those rows are errors on re-import — blank the ID
cells to recreate the people instead.

Remember: manual imports only **add and update**. Removing a volunteer or
membership is done in the app, never via a spreadsheet with rows deleted.
Photos are managed in the app and API only.

## Verify

The post-apply report states what was created and updated. Spot-check one
changed volunteer and one changed roster in the GUI. Since every write is
history-tracked, an unexpected result can be inspected (and understood) with
the as-of picker; the `changed_by` audit column records who ran the import.

## Scripted use

```sh
curl -s -H "Authorization: Bearer $TOKEN" -o parish.csv \
  https://vdb.sttimothyto.org/api/export/parish.csv
curl -s -H "Authorization: Bearer $TOKEN" -o my-teams.csv \
  https://vdb.sttimothyto.org/api/export/my-teams.csv
curl -s -H "Authorization: Bearer $TOKEN" -F file=@parish.csv \
  'https://vdb.sttimothyto.org/api/import?dry_run=true'
```

Drop `dry_run=true` to apply. See [Use the JSON API](api-recipes.md) for
obtaining `$TOKEN`.
