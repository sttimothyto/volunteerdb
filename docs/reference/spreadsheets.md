# Spreadsheet format

One `.xlsx` workbook, two sheets, designed to round-trip: export, edit in
any spreadsheet program, re-import. Implementation in
`src/volunteerdb/sheets/` (`common.py`, `exporter.py`, `importer.py`).
Uploads are capped at **10 MB**.

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

## Export variants

| File | Contents | Access |
|---|---|---|
| `template.xlsx` | Empty sheets with headers and a role dropdown | signed in |
| `parish.xlsx` | Every volunteer and membership (plus custom-field columns) | admin |
| `team/{id}.xlsx` | One team's roster | full-roster rights on the team |

All three accept `as_of=` over the API for historical snapshots. GUI entry
points: the `/import` page (admin) and the "Export roster" button on team
pages. See [Import and export spreadsheets](../how-to/import-export.md) for
the workflow and the {ref}`HTTP API reference <api-import-export>` for the
endpoints.
