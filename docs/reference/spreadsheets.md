# Spreadsheet format

One roster `.csv`, one row per person per team, designed to round-trip:
export, edit in any spreadsheet program, re-import. Implementation in
`src/volunteerdb/sheets/` (`common.py`, `exporter.py`, `importer.py`).
Uploads are capped at **10 MB**.

```{note}
Earlier releases used a two-sheet `.xlsx` workbook (Volunteers +
Memberships). That format is retired: uploading an `.xlsx` is rejected with
a pointer to export a fresh roster CSV. The same CSV also serves as the
nightly Google Drive roster sync format.
```

## Columns

| Column | Import behavior |
|---|---|
| First name | required |
| Last name | required |
| Email | matching key; may be blank or family-shared |
| Phone | free text |
| Volunteer notes | free text (on the person) |
| Active | allow-list both ways: `yes`/`y`/`true`/`1`/`x` or `no`/`n`/`false`/`0`. **Blank = leave unchanged** (new volunteers default to active). Anything else is a row **error** — a typo must never archive someone silently |
| Team | full path with ` / ` separator, e.g. `Liturgy / Music Ministry`; a bare unambiguous name also works. **Blank = volunteer-only row** (contact update without touching memberships) |
| Role | short value (`leader`) or display label (`Ministry leader`); required when Team is set |
| Joined on | ISO date (`2026-05-03`) |
| Membership notes | free text (on the membership) |

A volunteer serving on several teams appears once per team; the volunteer
columns of later rows update the same person (harmlessly, since the values
match). Parish-wide exports list volunteers with no membership at the end,
with blank team columns.

Exports append one column per active custom field (e.g. *Safeguarding
training*) after the ten base columns. The custom-field columns are
currently **ignored on import** with a warning — custom-field values are
edited in the app. Photos are managed in the app and API only; they do not
travel through spreadsheets.

## Matching rules

1. A row **with an email** is matched by that email and nothing else. When
   several volunteers share it (families do), the **name** breaks the tie.
2. A row **with no email** is matched by exact **full name**.
3. Teams are matched by full path, then by unambiguous bare name.

Unmatched volunteers are created; matched ones are updated.

```{important}
Rule 1 is not a fallback chain. If a row carries an email that matches nobody,
the name is **not** consulted — even an exact full-name match — and a second
volunteer is created. This is how you add a contact address for someone already
on file *by mistake*: the import reports a warning naming the existing person,
but it still creates the duplicate.

To attach an address to an existing contactless volunteer, set it in the app
(or via `PATCH /api/volunteers/{id}`) **before** importing a sheet that carries
it.
```

## What an import will not do

- **A blank cell never clears a field.** Only non-empty values are written
  back, so deleting a phone number in an export and re-importing is a no-op.
  Clear a field in the app instead. This protects against a truncated paste
  silently wiping contact details parish-wide. (This now includes `Active`:
  a blank cell leaves an archived volunteer archived.)
- **Custom field values are not imported.** They are exported for reference;
  extra columns are ignored with a warning.
- **`Joined on` values it cannot read are dropped**, with a warning, and the
  membership is imported without a date. Warnings do not block an import — a
  file whose dates are all `03/05/2026` applies successfully with every join
  date missing. Use ISO dates (`2026-05-03`).

## Encoding and safety

- Encoding is UTF-8; exports carry a BOM so Excel opens them correctly, and
  imports accept files with or without one. Non-UTF-8 files are rejected
  with an explicit error.
- Formula-injection protection: exported cell values that could be
  interpreted as formulas (leading `=`, `+`, `-`, `@`) are escaped with a
  quote, and imports strip it back.

## Import semantics

- **Add and update only** — a manual import never deletes volunteers or
  memberships. (The nightly
  [Drive roster sync](../how-to/drive-roster-sync.md) is the one deliberate
  exception: a team's synced sheet is treated as that team's complete
  roster.)
- **All-or-nothing** — any error rejects the entire file; nothing is
  written. The response is a row-by-row report of issues.
- **Dry run** — the GUI always validates first and shows the report before
  offering "Apply this import"; the API exposes the same via
  `POST /api/import?dry_run=true`.

## Scoped imports (leaders and seconds)

Admins import without restriction. A team leader or second-in-command may
import too, limited **row by row** to the teams they lead (sub-teams
included); out-of-scope rows are reported as errors and, as always,
any error blocks the whole file:

- **Rows with a Team** must target a managed team.
- **Volunteer columns** may update people who are on a managed team
  (evaluated against pre-import memberships, plus anyone the same file adds
  to a managed team).
- **New volunteers** are created only when a row also gives them a
  membership on a managed team.
- Out-of-scope volunteer rows are rejected even when they would change
  nothing — a dry-run must not confirm guessed contact details.

One quirk to know: volunteers are matched by email first, so a "new" row
that reuses an existing volunteer's email counts as an update of that
volunteer, and is rejected unless they are within scope.

## Export variants

| File | Contents | Access |
|---|---|---|
| `template.csv` | Header row only | signed in |
| `parish.csv` | Every membership row, then membership-less volunteers, plus custom-field columns | admin |
| `team/{id}.csv` | One team's roster (sub-teams included) | full-roster rights on the team |
| `my-teams.csv` | Union of the caller's managed teams | leads/seconds any team |

All data exports accept `as_of=` over the API for historical snapshots
(`my-teams` applies the *current* set of managed teams to the historical
data). GUI entry points: the `/import` page and the "Export roster (.csv)"
button on team pages. See
[Import and export spreadsheets](../how-to/import-export.md) for the
workflow and the {ref}`HTTP API reference <api-import-export>` for the
endpoints.
