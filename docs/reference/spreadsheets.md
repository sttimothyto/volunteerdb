# Spreadsheet format

One `.xlsx` workbook, two sheets, designed to round-trip: export, edit in
any spreadsheet program, re-import. A **CSV variant** carries one sheet per
file (see below). Implementation in `src/volunteerdb/sheets/` (`common.py`,
`exporter.py`, `importer.py`). Uploads are capped at **10 MB**.

## Sheet "Volunteers"

| Column | Import behavior |
|---|---|
| First name | required |
| Last name | required |
| Email | matching key; may be blank or family-shared |
| Phone | free text |
| Notes | free text |
| Active | boolean-ish (`yes`/`no`, `true`/`false`, `1`/`0`) |

Exports append one column per active custom field (e.g. *Safeguarding
training*). These extra columns are currently **ignored on import** with a
warning — custom-field values are edited in the app.

## Sheet "Memberships"

| Column | Import behavior |
|---|---|
| Volunteer email | primary matching key |
| Volunteer name | disambiguates family-shared emails; fallback key when email is blank |
| Team path | full path with ` / ` separator, e.g. `Liturgy / Music Ministry`; a bare unambiguous name also works |
| Role | short value (`leader`) or display label (`Ministry leader`); dropdown-validated in the template |
| Joined on | date |
| Notes | free text |

## CSV variant

Since CSV is single-table, the workbook splits into two files —
`volunteers.csv` and `memberships.csv` — with exactly the columns above.
On import the file's **header row** identifies which sheet it is; the
format itself is detected from the content (zip magic bytes → `.xlsx`,
otherwise CSV), so the extension is informational. Details:

- Encoding is UTF-8; exports carry a BOM so Excel opens them correctly, and
  imports accept files with or without one. Non-UTF-8 files are rejected
  with an explicit error.
- A CSV import touches only the sheet it carries — importing
  `volunteers.csv` never affects memberships, and vice versa.
- Extra columns beyond the standard headers are ignored with a warning,
  as in the workbook.
- Formula-injection escaping applies to CSV exactly as to `.xlsx`.
- The role dropdown of the `.xlsx` template cannot be expressed in CSV;
  the CSV templates are headers only.

## Matching rules

1. Volunteers are matched by **email**; when several volunteers share the
   email, the **name** breaks the tie.
2. With no email, an exact **full name** match is used.
3. Teams are matched by full path, then by unambiguous bare name.

Unmatched volunteers are created; matched ones are updated.

## Import semantics

- **Add and update only** — an import never deletes volunteers or
  memberships.
- **All-or-nothing** — any error rejects the entire file; nothing is
  written. The response is a row-by-row report of issues.
- **Dry run** — the GUI always validates first and shows the report before
  offering "Apply this import"; the API exposes the same via
  `POST /api/import?dry_run=true`.
- Formula-injection protection: exported cell values that could be
  interpreted as formulas are escaped, and imports sanitize them back.

## Scoped imports (leaders and seconds)

Admins import without restriction. A team leader or second-in-command may
import too, limited **row by row** to the teams they lead (sub-teams
included); out-of-scope rows are reported as errors and, as always,
any error blocks the whole file:

- **Memberships rows** must target a managed team.
- **Volunteers rows** may update people who are on a managed team
  (evaluated against pre-import memberships, plus anyone the same file adds
  to a managed team).
- **New volunteers** are created only when the same file also gives them a
  membership on a managed team.
- Out-of-scope volunteer rows are rejected even when they would change
  nothing — a dry-run must not confirm guessed contact details.

One quirk to know: volunteers are matched by email first, so a "new" row
that reuses an existing volunteer's email counts as an update of that
volunteer, and is rejected unless they are within scope.

## Export variants

| File | Contents | Access |
|---|---|---|
| `template.xlsx` | Empty sheets with headers and a role dropdown | signed in |
| `template/{sheet}.csv` | Headers only (`volunteers` or `memberships`) | signed in |
| `parish.xlsx` | Every volunteer and membership (plus custom-field columns) | admin |
| `parish/{sheet}.csv` | One sheet of the above | admin |
| `team/{id}.xlsx` | One team's roster | full-roster rights on the team |
| `team/{id}/{sheet}.csv` | One sheet of the above | full-roster rights on the team |
| `my-teams.xlsx` | Union of the caller's managed teams | leads/seconds any team |
| `my-teams/{sheet}.csv` | One sheet of the above | leads/seconds any team |

All data exports accept `as_of=` over the API for historical snapshots
(`my-teams` applies the *current* set of managed teams to the historical
data). GUI entry points: the `/import` page and the "Export roster" menu on
team pages. See [Import and export spreadsheets](../how-to/import-export.md)
for the workflow and the {ref}`HTTP API reference <api-import-export>` for
the endpoints.
