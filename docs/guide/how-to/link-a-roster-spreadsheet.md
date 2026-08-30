# Link a roster spreadsheet

Keep the roster of your team in a Google Sheet that you and your helpers edit, and that the site syncs every night.

## Before you start

- You are signed in as a leader or second-in-command of the team, or of a team above it.
- You look at today, not at a past date. The *Roster spreadsheet* section is hidden on an as-of view.
- You have a Google account.
- If you do nothing, the site makes a sheet for the team at the nightly sync (2:30), and the link appears in the section. Follow the steps to use your own copy of the template instead.

## Steps

### Make the sheet

1. Open *Teams* and click the team.
2. Under *Roster spreadsheet*, click *Roster template (Google Sheets)*.
3. In Google Sheets, click *File*, then *Make a copy*.
4. In the copy, click *Share*.
5. Choose *Anyone with the link*, with the right *Editor*. *Viewer* is not enough.
6. Copy the link of the copy.

### Link it

1. Back on the team page, click *Link a spreadsheet*.
2. Read the red warning in the dialog *Roster spreadsheet*.
3. Paste the link into *Google Sheets link*.
4. Leave *Overwrite it from the database* selected for a fresh copy of the template.
5. Choose *Import its rows into the database* only if the sheet already holds rows to keep.
6. Click *Save*.

## What you see

- The message *Syncing with Google Sheets…*, then a green message: *sheet rewritten from the database*, or the counts of the rows imported.
- The section shows the name of the sheet as a link, with *Change spreadsheet*, *Sync now* and *Overwrite sheet*. Under it is *Last synced*, with the date and time.
- The sheet has the 8 roster columns, a drop-down list in *Role* and in *Team*, a hidden *ID* column and a protected header row. See [The roster spreadsheet](../reference/roster-spreadsheet.md).

## Every night

- At 2:30 the site reads the sheet, adds and updates people from its rows, and then rewrites the sheet to match the roster.
- A sync never removes anybody. A row you deleted from the sheet comes back the same night. Take a member off the roster on the team page instead.
- A sync never clears a value. A blank cell leaves the value the site holds.
- If a row has an error, nothing from the sheet is saved that night. The sheet is left as it is, and the section says *Last sync failed:* with the row and the reason.
- Only the first tab is synced, and the rewrite can delete other tabs. For scratch work, make a copy of the sheet; a copy is never synced.
- In history, a change that came from the sheet is recorded under the sync's own account, not under your name.

## The 3 buttons

- *Sync now* runs the nightly sync at once: the sheet into the site, then the site back into the sheet.
- *Overwrite sheet* rewrites the sheet from the roster and discards whatever is in the sheet. Use it when the sheet is a mess.
- *Change spreadsheet* links a different sheet. The old sheet is not synced any more.

## Keep the link private

- Anyone who holds the link can open the sheet and edit it. There is no list of people; the link is the access.
- The sheet holds the email address, the phone number and the notes of every member, and an edit reaches the roster at the next sync.
- Give the link to the people who help run the team, and to nobody else.

## If something goes wrong

- *not a Google Sheets link*: copy the link from the address bar while the sheet is open. A Google Doc link is refused here.
- *that spreadsheet already belongs to* a team: a sheet syncs with exactly 1 team. Make a new copy of the template.
- *Linked, but the first sync failed:* with *the spreadsheet is not shared*: repeat the share step, then click *Sync now*.
- *the spreadsheet was not found*: the sheet was deleted, or the link is wrong.
- *Last sync failed: row* N: *this sheet only manages* your team: that row names another team. Remove the row, or blank its *Team* cell.
- *Last sync failed: row* N: *unknown team* or *Role is required when Team is set*: fix that row. Use the drop-down lists.
- *roster spreadsheet sync is not configured*: the parish has not set up Google Sheets yet. Tell the administrator.

## Related pages

- [Import a roster from a .csv file](import-a-csv.md)
- [Export the roster](export-the-roster.md)
- [Add or remove a member](add-or-remove-a-member.md)
- [The roster spreadsheet](../reference/roster-spreadsheet.md)
- [Spreadsheets and home pages](../explanation/spreadsheets-and-home-pages.md)
- Technical detail: [Sync team rosters with Google Sheets](../../how-to/roster-spreadsheets.md)
