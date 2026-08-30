# Spreadsheets and home pages

Many leaders already keep their roster in a spreadsheet, and many teams
already keep their notices in a Google Doc. The site does not ask them to
give those up. It reads the sheet every night and rewrites it to match its
own record. It reads the doc every night and publishes it as a page. The
leader keeps the tool they know, and the parish keeps one record.

## Why a team roster can live in a Google Sheet

A leader who has kept the choir list in a spreadsheet for 10 years will not
type it into a web page. So each team gets a Google Sheet, with one row for
each person: name, email, phone, notes, team and role. The leader edits it
in the browser, as before. The site makes the sheet for a team that has
none, or the leader links a copy of the *Roster template (Google Sheets)*.
Everyone who holds the link can edit the sheet, so the link is the key: it
belongs among the people who help run the team. That is why the *Roster
spreadsheet* section appears only to the team's leaders and seconds, and to
administrators.

## What the nightly sync does

Every night at 2:30 the site reads the sheet. A new row adds a person to the
roster, and a changed phone number or role updates the person. Then the site
rewrites the sheet from its own record, so by morning the two agree. A
change made in the site appears in the sheet the next morning. A hidden
column pins each row to its person, so a corrected email address updates
that person instead of making a twin. *Sync now* on the team page does the
same at once, and *Last synced* shows when the last sync ran.

## What the sync never does

A sync never removes anybody. Delete a row from the sheet, and the person is
back in it the next morning. A spreadsheet is edited by hand, with filters,
sorted views and pasted blocks. If a missing row meant a resignation, every
slip would lose a parishioner. A member leaves a team in the site, where the
change carries a name and a date and shows in the history. A blank cell
never clears a detail, either.

A sheet with a problem, for example an unknown role, is skipped whole. The
site applies nothing from it, leaves it untouched to be corrected, and shows
*Last sync failed* on the team page. Only the first tab is read, and the
rewrite can remove other tabs, so scratch work belongs in a copy. A sheet
nobody can repair is rescued with *Overwrite sheet*, which rewrites it from
the site's record. A task force never gets a sheet: its roster is borrowed
from other teams, and their details must not travel into a shared file.

## Why the home page comes from a Google Doc

A team wants a page for newcomers: rehearsal times, whom to call, a sign-up
link. The people who write it are not web designers, but they can edit a
Google Doc. So the team keeps the page in a doc, shared with anyone who has
the link, and pastes the link under *Volunteer home page*. Every night at
3:00 the site fetches the doc and publishes it, and *Fetch now* does it at
once. If the doc is deleted or made private, the last good version stays up,
and the team page shows *Last fetch failed*. A leader, a second or a core
member can set the doc: leaders are often elderly, and a page nobody can
refresh goes stale.

## What the public page shows and does not collect

The pages sit under *Ministries* and need no sign-in; the sign-in page
offers *Browse ministry home pages*. The index, *Ministry home pages*, lists
each team with a page. A team's page shows the team's name, the text and
pictures of the doc, and *Last updated* with a date. It asks nothing of the
reader: no name, no email, no account. It shows nothing but the doc, so the
page is exactly as public as the doc, and nothing private belongs in it.
Anything in the doc that could run, such as a script, is stripped before the
page is published.

*Download QR Code to Public page* gives a code to print in the bulletin or
on a poster. The code points at the page's address, which comes from the
team's name, so a renamed team needs a new print.

## Related pages

- [Link a roster spreadsheet](../how-to/link-a-roster-spreadsheet.md)
- [Import a roster from a .csv file](../how-to/import-a-csv.md)
- [Export the roster](../how-to/export-the-roster.md)
- [Publish the team home page](../how-to/publish-the-team-home-page.md)
- [The roster spreadsheet](../reference/roster-spreadsheet.md)
- [History and as-of dates](history-and-as-of.md)
- Technical detail: [Sync team rosters with Google Sheets](../../how-to/roster-spreadsheets.md)
- Technical detail: [Publish a team home page](../../how-to/team-home-pages.md)
- Technical detail: [Spreadsheet format](../../reference/spreadsheets.md)
