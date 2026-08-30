# Spreadsheet format

- One roster `.csv`, one row per person per team. The format round-trips:
  export, edit in any spreadsheet program, import again.
- The implementation is in `src/volunteerdb/sheets/` (`common.py`,
  `exporter.py`, `importer.py`).
- The upload cap is **10 MB**.

```{note}
Earlier releases used a 2-sheet `.xlsx` workbook (Volunteers +
Memberships), then a 10-column CSV with `Active`, `Joined on` and
`Membership notes` columns. Both are retired. The importer rejects an
`.xlsx` upload with a pointer to export a fresh roster CSV. An old-layout
CSV fails the header check ("download a fresh template or export"). The
same CSV also serves as the nightly Google Sheets roster sync format.
```

## Columns

| Column | Import behavior |
|---|---|
| ID | the volunteer's database id, written by every export. **Pins the row to that exact record** — leave blank for new people. A non-numeric or unknown ID is a row **error**, never a create |
| First name | required |
| Last name | required |
| Email | matching key for blank-ID rows; can be blank or family-shared. On an ID row, editing it *corrects* the address |
| Phone | free text |
| Volunteer notes | free text (on the person) |
| Team | full path with ` / ` separator, for example `Liturgy / Music Ministry`; a bare unambiguous name also works. **Blank = volunteer-only row** (contact update without touching memberships) |
| Role | short value (`leader`) or display label (`Ministry leader`); required when Team is set |

- A volunteer on several teams appears once per team. The volunteer columns
  of later rows update the same person, harmlessly, because the values
  match.
- A parish-wide export lists the volunteers with no membership at the end,
  with blank team columns.
- Exports hold **active volunteers only**. An archived volunteer with no
  membership appears in no export, because there is no Active column to
  mark them. An archived volunteer still on a team keeps their membership
  rows.
- Exports append one column per active custom field (for example
  *Safeguarding training*) after the 8 base columns.
- The importer currently **ignores the custom-field columns**, with a
  warning. Edit custom-field values in the app.
- The app and the API manage photos. Photos do not travel through
  spreadsheets.

## Matching rules

1. A row **with an ID** *is* that volunteer. The importer does not search
   for a match. If both names differ from the record, the importer warns
   ("check the ID"). The warning guards against a copy-pasted row that kept
   a stale ID.
2. The importer matches a blank-ID row **with an email** by that email and
   nothing else. When several volunteers share it (families do), the
   **name** breaks the tie.
3. The importer matches a blank-ID row **with no email** by exact
   **full name**.
4. The importer matches a team by full path, then by unambiguous bare name.

The importer creates unmatched volunteers and updates matched ones.

```{important}
Rule 2 is not a fallback chain. If a blank-ID row carries an email that
matches nobody, the importer does **not** consult the name, even an exact
full-name match. It creates a second volunteer. This is how you add an
address for someone already on file *by mistake*. The import reports a
warning that names the existing person, but it still creates the
duplicate.

To change someone's address safely, edit the Email cell of a row that
carries their ID (any export has it). Or set it in the app, or with
`PATCH /api/volunteers/{id}`, **before** you import a sheet that carries
it.
```

## What an import will not do

- **Clear a field from a blank cell.** A blank cell never clears a field.
  The importer writes only non-empty values. If you delete a phone number
  in an export and import it again, nothing changes. Clear a field in the
  app instead. This protects against a truncated paste that silently wipes
  contact details parish-wide.
- **Import custom field values.** Exports carry them for reference. The
  importer ignores the extra columns, with a warning.
- **Archive anyone.** There is no Active column: archive a volunteer in the
  app. There is one automatic transition in the other direction. A row that
  puts an archived volunteer **on a team** reactivates them, because a join
  implies active. The report says so. A bare contact-update row leaves the
  archive flag alone.

## Encoding and safety

- The encoding is UTF-8.
- Exports carry a BOM, so Excel opens them correctly. Imports accept files
  with or without one.
- The importer rejects a non-UTF-8 file with an explicit error.
- Formula-injection protection: a cell value with a leading `=`, `+`, `-`
  or `@` could read as a formula. The exporter escapes such a value with a
  quote, and the importer strips the quote again.

## Import semantics

- **Add and update only.** No path through the importer deletes a volunteer
  or a membership, the nightly
  [roster sync](../how-to/roster-spreadsheets.md) included. The write-back
  leg restores a row deleted from a team's sheet; the sync does not treat
  it as a resignation. Members leave a team in the app.
- **All-or-nothing.** Any error rejects the entire file, and the importer
  writes nothing. The response is a row-by-row report of issues.
- **Dry run.** The GUI always validates first and shows the report before
  it offers *Apply this import*. The API exposes the same through
  `POST /api/import?dry_run=true`.

## Scoped imports (leaders and seconds)

- Admins import without restriction.
- A team leader or second-in-command can import too, limited **row by row**
  to the teams they lead (sub-teams included).
- The importer reports out-of-scope rows as errors. As always, any error
  blocks the whole file.
- **Rows with a Team** must target a managed team.
- **Volunteer columns** can update people who are on a managed team. The
  importer evaluates that against pre-import memberships, plus anyone the
  same file adds to a managed team.
- The importer creates a **new volunteer** only when a row also gives them
  a membership on a managed team.
- The importer rejects out-of-scope volunteer rows even when they would
  change nothing. A dry run must not confirm guessed contact details.
- One quirk to know: a row that carries an ID, or that reuses an existing
  volunteer's email, counts as an update of that volunteer. The importer
  rejects it unless that volunteer is within scope.

## Sheet decoration

- The Google Sheets the nightly sync maintains, and the template sheet a
  team's **Roster spreadsheet** section links to, carry cosmetic
  guardrails.
- The sync's decorate leg re-applies them every night
  ([how-to](../how-to/roster-spreadsheets.md#sheet-decoration-self-healing)).
- The guardrails are:
  - a strict **Role dropdown** of the 4 display labels;
  - a strict **Team dropdown**;
  - a **hidden ID column** (still exported, so the pin survives);
  - a **frozen, warning-protected header row**;
  - a structure-warning note on the header.
- Decoration never touches cell values. It never affects what the sync
  reads or writes.
- A team's own sheet offers exactly **one** team in the Team dropdown: its
  own display path. The sync rejects a row that names any other team. The
  blank the column usually carries stays legal.
- The template offers every active team, because people copy it for hand
  imports.

## Export variants

| File | Contents | Access |
|---|---|---|
| `parish.csv` | Every membership row, then membership-less volunteers, plus custom-field columns | admin |
| `team/{id}.csv` | One team's roster (sub-teams included) | full-roster rights on the team |
| `my-teams.csv` | Union of the caller's managed teams | leads/seconds any team |

- All data exports accept `as_of=` over the API for historical snapshots.
  `my-teams` applies the *current* set of managed teams to the historical
  data.
- GUI entry points:
  - *Export team(s)* on `/teams`;
  - on a team's page, the *Export roster (.csv)* button;
  - on a team's page, the **Roster spreadsheet** section, with the
    *Roster template (Google Sheets)* link and the *Import a .csv* upload.
- There is no separate import page.
- See [Import and export spreadsheets](../how-to/roster-spreadsheets.md)
  for the workflow and the {ref}`HTTP API reference <api-import-export>`
  for the endpoints.
