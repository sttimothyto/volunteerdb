# Import and export spreadsheets

Bulk data moves through one `.xlsx` workbook that round-trips: export, edit,
re-import — or through single-sheet `.csv` files (`volunteers.csv`,
`memberships.csv`). Column semantics, matching rules, and limits are in the
[spreadsheet format reference](../reference/spreadsheets.md). The GUI lives
at **`/import`** (header → *Import/Export*) and is available to admins and
to team leaders/seconds; leader imports and exports cover only the teams
they lead (sub-teams included).

## Export

- **Full parish** — `/import` → *Full parish export*: every volunteer
  (including custom-field columns) and every membership. Admin only.
- **My teams** — for leaders/seconds, `/import` → *My teams export*: the
  union of their managed teams.
- **One team's roster** — the *Export roster* menu on a team page;
  available to anyone with full-roster rights on that team.
- **Empty template** — `/import` → *Empty template*: headers and a role
  dropdown, for building an import from scratch.

Each is offered as an `.xlsx` workbook or as `volunteers.csv` /
`memberships.csv`. Via the API, the data exports accept `as_of=` for
historical snapshots — see the {ref}`endpoints <api-import-export>`.

## Import

1. Prepare the file — an export is the best starting point for bulk edits;
   the template for new data. A `.csv` carries one sheet, identified by its
   header row, and only touches that sheet's data.
2. On `/import`, upload the file. The app **always dry-runs first**: nothing
   is written, and you get a row-by-row report of what would be created,
   updated, and any problems.
3. Read the report. Any error (unknown team, ambiguous volunteer, invalid
   role, …) blocks the whole file — imports are all-or-nothing, so fix the
   spreadsheet and re-upload rather than hoping for partial application.
   For leaders/seconds, rows outside their teams are errors too, and a new
   volunteer needs a membership row on one of their teams in the same file.
4. When the report is clean, click **Apply this import**.

Remember: imports only **add and update**. Removing a volunteer or
membership is done in the app, never via a spreadsheet with rows deleted.

## Verify

The post-apply report states what was created and updated. Spot-check one
changed volunteer and one changed roster in the GUI. Since every write is
history-tracked, an unexpected result can be inspected (and understood) with
the as-of picker; the `changed_by` audit column records who ran the import.

## Scripted use

```sh
curl -s -H "Authorization: Bearer $TOKEN" -o parish.xlsx \
  https://vdb.sttimothyto.org/api/export/parish.xlsx
curl -s -H "Authorization: Bearer $TOKEN" -o volunteers.csv \
  https://vdb.sttimothyto.org/api/export/my-teams/volunteers.csv
curl -s -H "Authorization: Bearer $TOKEN" -F file=@parish.xlsx \
  'https://vdb.sttimothyto.org/api/import?dry_run=true'
```

Drop `dry_run=true` to apply. See [Use the JSON API](api-recipes.md) for
obtaining `$TOKEN`.
