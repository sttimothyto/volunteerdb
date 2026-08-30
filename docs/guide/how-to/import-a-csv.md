# Import a roster from a .csv file

Add or update many people at once from a spreadsheet file.

## Before you start

- You are signed in as a leader or second-in-command of the team.
- You have a `.csv` file in the roster layout. Start from an export of the team, or from *Roster template (Google Sheets)*. See [Export the roster](export-the-roster.md).
- The file is smaller than 10 MB.

## The 8 columns

| Column | What to put in it |
|---|---|
| *ID* | The number the site gave the person. Keep it as exported. Leave it blank for a new person. |
| *First name* | Required. |
| *Last name* | Required. |
| *Email* | Their address. On a row with a blank *ID*, the site uses it to find the person. |
| *Phone* | Free text. |
| *Volunteer notes* | Notes about the person, shown to leaders only. |
| *Team* | The full name of the team, for example `Liturgy / Music Ministry`. Blank means: update the person, leave their teams alone. |
| *Role* | `Ministry leader`, `Second-in-command`, `Core team member` or `Member`. Required when *Team* is filled in. |

A blank cell never clears a field. To clear a phone number, edit the person in the app.

## Steps

1. Open *Teams* and click the team.
2. Under *Roster spreadsheet*, find *Import a .csv*.
3. Drop your file on the box *Drop a .csv file here (validated before anything is written)*.
4. Or click the box and pick the file.
5. Read the report. Nothing is written yet.
6. If the report lists errors, fix the file and upload it again.
7. When the report is clean, click *Apply this import*.

## What you see

- First, *Dry run — nothing written yet.*, with counts of the people and memberships the import would create or update.
- Warnings, marked with a warning sign, do not stop the import. They flag possible duplicates and suspect IDs. Read them.
- An error, marked with a cross, stops the whole file: *Not applied — fix the errors below and re-upload.*
- After *Apply this import*: *Import applied ✔*, and the message *Imported* with the file name.
- An import only adds and updates. It never removes a person from a team, and it never archives anybody.
- Rows are limited to the teams you lead. A new person must be put on one of your teams in the same file.
- A row that puts an archived person on a team makes them active again. The report says so.

## If something goes wrong

- *cannot identify CSV: the header row does not match the roster template*: start again from a fresh export or template.
- *first and last name are both required*: fill in both cells.
- *unknown team*: type the full name of the team exactly as the *Teams* page shows it.
- *Role is required when Team is set*: add a role, or blank the team.
- *ID ... matches no volunteer*: blank the *ID* cell to add the person as new.
- *new volunteers must be put on a team you lead (Team column)*: give the new person a *Team* and a *Role*.
- A team name followed by *is not a team you lead*: remove that row.

## Related pages

- [Link a roster spreadsheet](link-a-roster-spreadsheet.md)
- [Export the roster](export-the-roster.md)
- [Add or remove a member](add-or-remove-a-member.md)
- [The roster spreadsheet](../reference/roster-spreadsheet.md)
- Technical detail: [Spreadsheet format](../../reference/spreadsheets.md)
- Technical detail: [Sync team rosters with Google Sheets](../../how-to/roster-spreadsheets.md)
