# Import and export spreadsheets

Bulk data moves through one `.xlsx` workbook that round-trips: export, edit,
re-import. Column semantics, matching rules, and limits are in the
[spreadsheet format reference](../reference/spreadsheets.md). Import is
admin-only; the GUI lives at **`/import`** (header → *Import/Export*).

## Export

- **Full parish** — `/import` → *Full parish export*: every volunteer
  (including custom-field columns) and every membership. Admin only.
- **One team's roster** — the *Export roster* button on a team page;
  available to anyone with full-roster rights on that team.
- **Empty template** — `/import` → *Empty template*: headers and a role
  dropdown, for building an import from scratch.

Via the API, the same three exports accept `as_of=` for historical
snapshots — see the {ref}`endpoints <api-import-export>`.

## Import

1. Prepare the workbook — a full export is the best starting point for bulk
   edits; the template for new data.
2. On `/import`, upload the file. The app **always dry-runs first**: nothing
   is written, and you get a row-by-row report of what would be created,
   updated, and any problems.
3. Read the report. Any error (unknown team, ambiguous volunteer, invalid
   role, …) blocks the whole file — imports are all-or-nothing, so fix the
   spreadsheet and re-upload rather than hoping for partial application.
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
curl -s -H "Authorization: Bearer $TOKEN" -F file=@parish.xlsx \
  'https://vdb.sttimothyto.org/api/import?dry_run=true'
```

Drop `dry_run=true` to apply. See [Use the JSON API](api-recipes.md) for
obtaining `$TOKEN`.
