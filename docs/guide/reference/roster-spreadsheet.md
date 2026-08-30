# The roster spreadsheet

The roster spreadsheet and the roster `.csv` file share 1 layout: 8 columns, and 1 row for each person on each team. A volunteer on 3 teams has 3 rows.

## The 8 columns

| Column | What goes in it | If the cell is blank |
|---|---|---|
| *ID* | The number the site gave the volunteer. Every export fills it in. A number the site does not know is an error. | The site looks for the person by email, or by name when *Email* is blank too. It creates a new volunteer if it finds nobody. |
| *First name* | The first name. Required. | The row is an error. Nothing from the file is saved. |
| *Last name* | The last name. Required. | The row is an error. Nothing from the file is saved. |
| *Email* | The email address. Families can share 1 address. | The site keeps the address it has. On a row with no *ID*, it looks for the person by name instead. |
| *Phone* | The phone number, in any form. | The site keeps the number it has. |
| *Volunteer notes* | Notes about the person, the same on every row of that person. | The site keeps the notes it has. |
| *Team* | The full name of the team, with ` / ` between a team and its sub-team: `Liturgy / Music Ministry`. A short name works when only 1 team has it. | In a `.csv` file the row updates the person's contact details only; their teams do not change. If *Role* is filled in, the row is an error. In a team's own Google Sheet a blank cell means that team. |
| *Role* | *Ministry leader*, *Second-in-command*, *Core team member* or *Member*. In a `.csv` file the short forms `leader`, `second`, `core` and `member` also work. | If *Team* is filled in, the row is an error. |

## Rules for a blank cell

- A blank cell never clears a value the site already holds. To remove a phone number or a note, edit the volunteer in the app.
- A row with an *ID* is that exact person, whatever the names say. If both names differ from the record, the report warns you to check the *ID*.
- A row without an *ID* but with an email address is matched by that address alone. If 2 people share the address, the full name decides between them. If nobody has the address, the site creates a new volunteer, even when the name matches someone; the report warns you.
- A row without an *ID* and without an email address is matched by the full name.

## Extra columns

- A `.csv` export adds 1 column after the 8 for each custom field, for reference only. An import ignores these columns and says so in the report. A team's Google Sheet has no custom field columns.
- A team's own Google Sheet has a drop-down list in *Role* and in *Team*, and a hidden *ID* column. Its header row warns you before you change it. These come back every night.

## What a sync or an import never does

- It never removes anyone from a team. A row deleted from the sheet comes back on the next sync. Take a member off the roster in the app.
- It never deletes a volunteer, and it never archives one.
- It never clears a field.
- It never changes a custom field.
- It never saves half a file. 1 error in any row, and nothing is saved. The report lists every problem by row.
- A row that puts an archived volunteer on a new team brings them back to active. The `.csv` report says so.

## Other rules

- A team's Google Sheet holds that team only. A row that names another team is an error.
- Only the first tab of a Google Sheet syncs.
- A leader or second can import rows for their own teams only. A new person in the file must be put on one of those teams.
- Capitals do not matter in an email address or a name.
- If 2 rows give the same person 2 phone numbers, the last row wins.
- A `.csv` file can be up to 10 MB. An Excel file is refused: save it as `.csv` first.
- An export made by a core member leaves *Volunteer notes* blank. A team's Google Sheet always carries the notes.

## Related pages

- [Link a roster spreadsheet](../how-to/link-a-roster-spreadsheet.md)
- [Import a CSV](../how-to/import-a-csv.md)
- [Export the roster](../how-to/export-the-roster.md)
- [Spreadsheets and home pages](../explanation/spreadsheets-and-home-pages.md)
- Technical detail: [Spreadsheet format](../../reference/spreadsheets.md)
