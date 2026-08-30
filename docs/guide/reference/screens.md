# The screens

Every page of the site: what is on it, who sees it, and what each control does. The header is the same on every page, so it comes first.

The *Who* column uses these short names:

- *everyone*: everyone who is signed in.
- *members*: the people on the team's roster, in any role.
- *core*: the core members, leaders and seconds of the team, and the leaders and seconds of its parent teams.
- *leaders*: the leaders and seconds of the team, and the leaders and seconds of its parent teams.
- *admin*: administrators. An administrator sees everything and can do everything, on every team.

## The header

- The header sits at the top of every page. On a narrow screen the page names fold into 1 *Menu* button.
- The first link, *Skip to content*, appears when you press the Tab key. It jumps past the header.

| Control | What it does | Who |
|---|---|---|
| The logo | Shows the parish logo. A click opens the dialog *Site logo*. | Everyone sees it; *admin* can click it. |
| *Dash* | Opens the *Dashboard*. | *everyone* |
| *Teams* | Opens the *Teams* page. | *everyone* |
| *Volunteers* | Opens the *Volunteers* page. | *everyone* |
| *Events* | Opens the *Events* page. | *everyone* |
| *Elections* | Opens the *Elections* page. | *admin*, *leaders*, and anyone on the roll of an election |
| *Accounts*, *Fields*, *Workload* | Open the 3 administrator pages. | *admin* |
| Your email address | Opens your own profile. It is hidden on a narrow screen. | *everyone*; it is a link only for an account with a linked volunteer record |
| Your photo | Opens the dialog *Photo — …* to add, change or remove your photo. | *everyone* with a linked volunteer record |
| The gear *Settings* | Opens the settings menu, below. | *everyone* |
| *Sign out* | Signs you out and shows the sign-in page. | *everyone* |

The settings menu holds:

| Control | What it does |
|---|---|
| *Dark mode* | Switches the whole site between light and dark colours. The choice stays with this browser. |
| *Password & sign-in* | Opens *Your account*. |
| *Manual* | Opens this manual in a new tab. |
| *View as of (YYYY-MM-DD)* and *View* | Show the page as it was on a past date. Only the *Dashboard*, the *Teams* page and a team page have it. |

- With a past date set, the gear turns amber. The page shows the banner *Read-only snapshot as of …* with the button *Back to now*.
- An administrator also sees a banner when the site nears its email limit. It reads *Email sending is heading over its limit*, or *Email sending is over its limit*.

## Dashboard

The *Dashboard* is the first page after sign-in. Its bands run from the whole parish down to you. A band you have no right to is absent, not empty.

- At the top is the search box *Find volunteers or teams…* with the button *Search*.
- After 2 letters, a list of suggestions opens under the box: up to 6 under *Teams*, up to 6 under *Volunteers*.
- A click on a team opens its page. A click on a volunteer opens the side panel. *See every match for “…”* opens the *Volunteers* page.
- Enter, or *Search*, opens the *Volunteers* page with the search.

### Parish

Shown to *admin* only. Every tile but *Assignments* is a link.

| Tile | What it counts | Opens |
|---|---|---|
| *Active volunteers* | The volunteers marked active. Under it: *N inactive*. | *Volunteers* |
| *Active teams* | The teams that are not archived. | *Teams* |
| *Assignments* | The places on rosters. Under it: *N per volunteer*. 1 person on 3 teams counts 3 times. | — |
| *On no team* | The active volunteers on no roster. It turns amber when it is not 0. | *Volunteers* |
| *Can sign in* | The active volunteers with an account. Under it: *of N volunteers*. It is absent on a past date. | *Accounts* |

### Needs attention

Shown to *admin*, *leaders* and *core*. The figures cover the teams you help run; for an administrator, the whole parish.

| Tile or line | What it shows | Who |
|---|---|---|
| *Teams I help run* | The teams you have full-roster rights on. | *leaders*, *core* |
| *People on them* | The active people on those teams. | *leaders*, *core* |
| *No email address* | The people on those teams with no email address. They cannot be invited or emailed. | *admin*, *leaders*, *core* |
| *Without a leader*, *Without a second* | The teams you manage with that seat empty. | *admin*, *leaders* |
| *Shifts short of people* | The events of the next 30 days with fewer people than places. An event with an unlimited slot never counts. Absent on a past date. | *admin*, *leaders* |
| *Gaps:* | Up to 5 teams with an empty seat, each with *no leader* or *no second*. Each is a link to the team. | *admin*, *leaders* |
| *Workload:* | 1 chip per workload band with the number of people in it. A click opens the *Volunteers* page filtered by that band. | *admin*, *leaders* |
| *Open seats:* | The open elections by phase: *Nominating*, *Voting*, *Awaiting decision*. A click opens *Elections*. Absent on a past date. | *admin*, *leaders*, voters |

On a past date the band carries a note. It says that the counts are as of the snapshot, and that shifts, elections and sign-ins are left out.

### My teams

- Shown when your account is linked to a volunteer on at least 1 team.
- 1 row per team: the team's name and your role on it. A click opens the team page.

### My service

Shown when your account is linked to a volunteer, and not on a past date.

| Tile | What it shows | Opens |
|---|---|---|
| *Upcoming duties* | Your shifts still to come. Under it: *next* with the date and time of the first. | *Events* |
| *Shifts I could cover* | The open substitute requests on your teams. Absent when there are none. | *Events* |
| *Ballots waiting* | The elections you can vote in and have not. Absent when there are none. | *Elections* |
| *Hours served* | Your hours at past events. Under it: *N events attended*. | — |

### The ministry graph

- The graph draws the teams whose roster names you can see, and the people on them.
- *Focus on team* narrows the graph to 1 team and its sub-teams. *— whole parish —* shows everything you can see.
- The button *Fit the whole graph in view* brings the whole graph back on screen.
- Zoom in to read names. Hover over a node to see only its connections.
- A click on a team opens its page. A click on a volunteer opens the side panel.
- The legend: *team*, *volunteer*, *leadership* (a line from a leader or second to their team), *sub-team* (a line from a team to its parent).
- *admin* and *leaders* also see 1 legend entry per workload band. The volunteers whose workload they can read are coloured by band.

### Guides

- The last band, *Guides*, lists the pages of this manual for what you can do. Each link opens in a new tab.
- The groups: *For everyone*, *For team members*, *For voters*, *For core members*, *For leaders and seconds*, *For administrators*. *For team members* needs a linked volunteer record; *For voters* needs a place on a roll.
- The badge *Tutorial* marks a tutorial. The last link for administrators, *The technical manual*, opens the manual with the technical pages shown.

## Teams

Shown to *everyone*. It can show a past date.

| Control | What it does | Who |
|---|---|---|
| *Search teams…* | Narrows the table as you type. A parent's name keeps its sub-teams. | *everyone* |
| *View Team Homepages* | Opens the public *Ministries* pages. | *everyone* |
| *New team* | Opens the dialog *New team*: *Name*, *Parent team* (*— top level —* or a team), *Description*, *Workload weight*, *Cancel*, *Save*. | *admin*, not on a past date |
| *Export team(s)* | Downloads a `.csv` file with the rosters of the teams you have full-roster rights on. An administrator gets the whole parish. | *admin*, *leaders*, *core* |

The table:

- The *Team* column is the tree. A sub-team is indented under its parent, with └ before its name. An archived team carries the badge *inactive*.
- *admin* and *leaders* also see the columns *Ministry leader*, *Second-in-command*, *Core team member*, *Member*, *Total* and *Gaps*. The counts are filled in for the teams they manage only.
- *Gaps* shows the badges *no leader* and *no second*.
- A click on a column heading sorts. Drag a heading to move the column; the order stays until you sign out.
- A click on a row opens the team page. Under the table: *N teams*, or *N of M teams* while a search is on.

## Team page

The title is the team's full name, with its parent teams before it. Under it: the description, and the badge *inactive* for an archived team. It can show a past date.

| Control | What it does | Who |
|---|---|---|
| *Edit team* | Opens the dialog *Edit team*: the fields of *New team*, plus the switch *Active*. Off, the team is archived. | *admin*, not on a past date |
| *Delete* | Deletes the team and every place on its roster. The history keeps them. | *admin*, not on a past date |
| *Export roster (.csv)* | Downloads this roster as a `.csv` file. A core member gets it without the notes. | *core* |
| *Copy email list* | Copies every email address on the roster. | *core*, not on a past date |
| *Email all (BCC)* | Opens your mail app with everyone in *BCC*. | *core*, not on a past date |
| *View public homepage* | Opens the team's public page. Shown to a reader who is not *core*, when the page is published. | *everyone* |

The sections, from the top:

- *Service anniversaries — …*: an amber banner, shown to *leaders*. It names each member whose whole years on this team fall within the last 7 or next 30 days.
- *Volunteer home page* (*core*, not on a past date). *Set home page doc* opens the dialog *Team home page doc*, with the field *Google Doc link*, *Clear* and *Save*. With a doc set, the section shows the links *Google Doc* and *Public page*. It also shows the buttons *Download QR Code to Public page*, *Fetch now* and *Change*. Under them: *Not published yet — …*, *Refreshed nightly · last fetched …*, or *Last fetch failed: …*.
- *Add member* (*leaders*): the lists *Volunteer* and *Role*, and the button *Add*.
- *Roster* (*members*): 1 row per person. The name opens the side panel. *core* also see the email address and the phone number.
- The role is a badge. For *leaders* it is a list, and a new choice changes the role at once. They also see the icon *Remove from team* at the end of the row.
- Every member sees the account badge: *no account*, *disabled*, *invite sent*, *invite expired* or *account*. Next to it: *never signed in*, or *last login* with the date.
- For *core*, the badges *no account* and *invite expired* turn into a button on hover: *invite to create account* or *send a new invite*.
- A reader not on the team sees *You are not on this team, so its roster is not visible to you.* An empty roster says *Nobody on this team yet.*
- *Roster spreadsheet* (*leaders*, not on a past date). *Link a spreadsheet* opens the dialog *Roster spreadsheet*. The dialog has *Google Sheets link*, the choice *Overwrite it from the database* or *Import its rows into the database*, and *Save*. With a sheet linked, the section shows the link to the sheet, *Change spreadsheet*, *Sync now* and *Overwrite sheet*. It also shows *Roster template (Google Sheets)*. Under them: *Last synced …* or *Last sync failed: …*.
- *Import a .csv* (*leaders*, in the same section): the box *Drop a .csv file here (validated before anything is written)*. A clean file gets the report *Dry run — nothing written yet.* and the button *Apply this import*. A file with problems gets *Not applied — fix the errors below and re-upload.* and 1 line per problem. After the apply: *Import applied ✔*.
- *Sub-teams*: 1 button per sub-team.
- *Upcoming events* (*members*, not on a past date): the next 5 events with their date and *N/M filled*, and the link *All N upcoming events*.

## Volunteers

Shown to *everyone*. Everyone can search every name; the details of people outside your teams show as *•••*.

| Control | What it does | Who |
|---|---|---|
| *Search volunteers…* and *Search* | Search by name, and by email, phone, notes or custom field among the people you can see. The suggestion list works as on the *Dashboard*. | *everyone* |
| *Workload* | A list of the workload bands. Filters the table to 1 band. | *admin*, *leaders* |
| *New volunteer* | Opens the dialog *New volunteer*: *First name*, *Last name*, *Email*, *Phone*, *Create*. It then opens the new profile. | *admin* |
| *Matching teams* | Buttons for the teams whose names match the search. Each opens the team page. | *everyone* |

The table:

- The columns: *Name*, *Email*, *Phone*, then *Workload* for *admin* and *leaders*, then 1 column per custom field marked *in list*, then *Status*.
- *Workload* shows a badge with the band and the score, coloured by band, for the people whose workload you can read.
- *Status* shows *inactive* for an archived volunteer. Only *admin* sees archived volunteers in the table.
- 20 rows per page. A click on a row opens the side panel. Drag a heading to move a column.
- Under the table: *N volunteers*.

## Volunteer page

The title is the person's name. A reader who is not *core* on one of the person's teams sees the name, the sign-in status, the teams and the timeline only.

| Control | What it does | Who |
|---|---|---|
| The photo | Opens the dialog *Photo — …*: the box *Drop a headshot here (stored as 400×400 JPEG)*, a declaration to tick, *Remove photo* and *Upload*. | *everyone* |
| *Edit* | Opens the dialog *Edit …*: *First name*, *Last name*, *Email*, *Phone*, *Notes*, 1 field per custom field, *Cancel*, *Save*. *admin* also gets the switch *Active*. | the person, *leaders*, *admin* |
| *Delete* | Asks *Delete this volunteer and all their memberships?*, then removes the person. The history keeps them. | *admin* |
| The icon *Remove from team* | Takes the person off that team, on the row under *Serves on*. | *leaders* of that team |
| *Add to team* | The lists *Team* and *Role*, and the button *Add*. The list holds the teams you manage. | *leaders*, *admin* |

The sections, from the top:

- The top card: the photo, the name, the badge *inactive*, and for *admin* and *leaders* the badge *workload: band · score*.
- Under them, for *core* and the person: *Email:*, *Phone:*, 1 line per custom field, and *Service hours: N h across N events*. *Notes:* shows for *leaders*, *admin* and the person. Others see *Contact details visible to their team leaders and core members.*
- *Last login:* the date and time, *never signed in*, *no VolunteerDB account*, or the date with *— account disabled*. Everyone sees it. Next to it, *core* see *invite to create account* or *send a new invite* when an invitation makes sense.
- In the *Edit* dialog, your own *Email* is different. The site sends a confirmation link to the new address, and nothing changes until you open it.
- *Serves on*: 1 row per team, with the role. *Not on any team.* when there is none.
- *Service timeline*: a chart with 1 bar per team and per spell of service, coloured by role, over the years.
- *If they leave, what vacancies appear?* (*core*): 1 row per team. Each row carries the badge *team left with NO leadership* or *no leader left (second remains)*, or the line *N leader(s), N leadership total remain*. With no team: *No memberships — no holes.*
- *Proposals involving them*: 1 row per election you can see, with the badges *Appointed*, the phase, *Candidate* and *Voting member*.

## The volunteer panel

The side panel slides in from the right. It opens from the *Dashboard* graph and search, the *Volunteers* table, a roster row, and the names on event pages.

- At the top: the photo (a click opens the photo dialog), the name, and the button *Close*.
- Under the name: the badge *inactive*, and for *admin* and *leaders* the badge *workload: band · score*.
- For *core* and the person: *Email:*, *Phone:*, 1 line per custom field, and *Notes:* for *leaders*, *admin* and the person. Others see *Contact details visible to their team leaders and core members.*
- *Last login:* with the same values as on the profile, and the invite control for *core*.
- *Serves on*: 1 line per team with the role. Each team is a link. *Not on any team.* when there is none.
- *Full profile* opens the volunteer page.
- On a past date the panel shows the person as they were then, and the photo cannot be changed.

## Events

Shown to *everyone*. The list holds the events of the teams whose roster names you can see; for *admin*, every team.

| Section or control | What it shows or does | Who |
|---|---|---|
| *Your upcoming duties* | 1 row per shift: the event, the slot, the date and time. *Need a sub* opens the dialog *Ask for a substitute* with *Note to the team (optional)* and *Ask the team*. With a request out: the badge *sub wanted* and *Withdraw request*. | *everyone* with a linked volunteer and a shift |
| *Teammates need a substitute* | 1 row per open request on your teams: who *needs a* slot *at* which event, their note, and *Take this slot*. | *members* not already at that event |
| *My duties* / *Whole parish* | Switches the calendar between your shifts and every team's events. | *everyone* |
| *Add to your calendar* | Opens a panel with a link to subscribe your own calendar and a `.ics` file to download. Your own feed also has the *Feed address* to paste and *Reset the address*. The parish panel has *Add to Google Calendar* once the parish calendar exists. | *everyone*; in the *Whole parish* panel, *admin* also sees the state of the parish Google calendar |
| The month grid | 1 cell per day, Sunday first, with the time and name of each event. A link at each end, *← July* for example, moves to the month before or after. | *everyone* |
| *Upcoming events on your teams* | The heading of the table. For *admin* it reads *Upcoming events (all teams)*. With *Show past* on, *Upcoming* becomes *Past*. | *everyone* |
| *Search events…* | Narrows the table as you type, on the name, the team, the place and the date. | *everyone* |
| *All teams* | A list to show 1 team's events. Shown when the table holds more than 1 team. | *everyone* |
| *Show past* / *Show upcoming* | Switches the table between events to come and past or cancelled events. | *everyone* |
| *New event* | Opens the dialog *New event*, below. | *admin*, *leaders* |

The dialog *New event*:

- *Team*, *Title*, *Date (YYYY-MM-DD)*, *Starts (HH:MM)*, *Ends (HH:MM)*, *Location (optional)*, *Description (optional)*.
- 1 row per slot: *Slot* and *Capacity*. A blank capacity means unlimited. *Add another slot* adds a row.
- *Repeat weekly until (YYYY-MM-DD, optional)* makes 1 event per week up to that date.
- *Create event* saves and opens the event. If a similar event exists at that place on that day, the dialog *Possible double booking* asks *Go back* or *Create anyway*.

The table:

- The columns: *When*, *Event*, *Team*, *Location*, *Filled* (a bar and *N/M*; *∞* means unlimited), *You* (*serving*, *available* or *unavailable*).
- A cancelled event carries *(cancelled)* after its name, in the past list only.
- A click on a row opens the event page. The past list shows 20 rows per page. Under the table: *N events*.
- With no events: *Nothing scheduled yet.*

## Event page

The title is the event's name. Under it: the team (a link), a badge with the date, *Past* or *Cancelled*, the date and time, and the place.

| Control | What it does | Who |
|---|---|---|
| *Share* | Opens the panel *Event link* with the address, *Copy* and *Close*. The link asks for a sign-in. | *everyone* who can see the event |
| *Edit* | Opens the dialog *Edit event*: *Title*, *Date (YYYY-MM-DD)*, *Starts (HH:MM)*, *Ends (HH:MM)*, *Location*, *Description*, *Save*. | *leaders*, before the event is cancelled |
| *Cancel event* | Asks *Keep it* or *Yes, cancel it*. Everyone signed up is emailed, if the event has not ended yet. | *leaders*, before the event is cancelled |
| *Add slot* | Opens the dialog *Add a slot*: *Slot name*, *Capacity (blank = unlimited)*, *Description (optional)*, *Add slot*. | *leaders*, event still to come |

The sections, from the top:

- *Collaboration* (*leaders*, event still to come): the badge *task force* and *Staffed by: …* once a second team is in, with *Sync rosters*. The list *Add collaborating team* and the button *Add* bring another team's roster in; the site asks *Add team* first.
- *Can you serve at this event?* (*members*, event still to come): *Note (optional)*, *Available*, *Not available*. Your answer shows as the badge *you said: available* or *you said: not available*.
- *Slots*: 1 card per slot, with the name, the badge *N/M* (*∞* means unlimited), the description, and 1 row per person.
- *Substitutes wanted* (*members* with no shift at the event): *X needs a* slot, the note, and *Take this slot*.
- *Availability answers* (*leaders*): 1 row per answer, *available* or *not available*, with the note.
- *Attendance* (*leaders*, after the event): 1 row per person with the slot, the box *attended*, the field *hours* and *Save*. Once a row is changed, it carries the badge *adjusted* and the button *Reset*. Everyone assigned counts as attended for the planned hours unless corrected here.

On a slot card:

| Control | What it does | Who |
|---|---|---|
| *Sign up* | Opens the dialog *Sign up — …*: *Also sign me up for the later weeks of this series* (weekly events only), *Email me a reminder:* *7 days before* and *24 hours before*, and *Sign up*. | *members* with no shift at the event, while there is space |
| The pencil icon | Opens the dialog *Edit slot* with the name, the capacity and the description. | *leaders*, event still to come |
| The bin icon *Remove this empty slot* | Removes a slot with nobody on it. The last slot of an event cannot be removed. | *leaders*, event still to come |
| A name | Opens the side panel. Badges: *substitute*, *marked unavailable*, *sub wanted*. | everyone who can see the event |
| *Need a sub* | Opens the dialog *Ask for a substitute*. | the person on that row |
| *Hand off* | Opens the dialog *Hand this slot to a teammate*: the list *Who takes it?* and *Hand it over*. The teammate is emailed. | the person on that row |
| *Withdraw* | Opens the dialog *Take yourself off this slot*: *Why can you no longer serve?* and *Take me off*. The leaders are emailed the reason. | the person on that row |
| *Remove* | Takes that person off the slot. | *leaders* |
| *Schedule someone* and *Assign* | A list of the roster, the people who said *available* first, and the button that puts the chosen person on the slot. | *leaders*, while there is space |

A reader who cannot see the event sees *This event is visible to the members of its team.*

## Elections

Shown to *admin*, *leaders*, and anyone on the roll of an election. Others see *Elections are available to admins, team leaders/seconds, and the voting members of a proposal.*

| Section | What it shows | Who |
|---|---|---|
| *Open proposals* | 1 row per open election: the team and the seat, a phase badge, and *N candidates · N/N ballots*. A click opens the election page. | everyone on the page |
| *Vacancies* | 1 card per team with an empty seat: the team, the badges *no leader*, *no second-in-command* and *proposal open*, *N members*, and *Start proposal*. With no gaps: *Every team has a leader and a second-in-command. 🎉* | *admin*, *leaders* |
| *Recently decided* | The 20 newest elections that were appointed or cancelled. | everyone on the page |

- A voter with nothing open sees *No open proposals need you right now.*
- The phase badges: *Nominating until* a date, *Voting until* a date, *Awaiting decision*, *Appointed*, *Cancelled*.

The dialog *Propose for …*, opened by *Start proposal*:

- *Role* (the empty seat is chosen for you), *First candidate*, *Why them?*.
- *Nominations close (YYYY-MM-DD)*, 14 days ahead as standard, and *Voting closes (YYYY-MM-DD)*, 28 days ahead as standard.
- *Notes (what is this seat, and why now?)*: shown to the voters on the election page.
- *Create proposal* saves and opens the election. The roll starts with the team's leader, second and core members, plus the *Clergy* team.

## Election page

The title is the team and the seat. Under it: the team (a link), the seat and the phase badge. Then *Nominations close* and *Voting closes* with their dates, *opened by* and *decided by* with an email address, and the notes.

| Control | What it does | Who |
|---|---|---|
| *Edit deadlines & notes* | Opens the dialog *Edit proposal*: the 2 dates, *Notes*, *Save*. | *leaders*, while the election is open |
| *Cancel proposal* | Asks *Keep it* or *Yes, cancel it*. | *leaders*, while the election is open |
| *Remove* on a candidate | Takes the candidate off the list. | *leaders*, while nominations are open |
| *Appoint* on a candidate | Asks *Yes, appoint*, then gives the candidate the seat at once. | *leaders*, under *Awaiting decision* |
| *New candidate*, *Why them?*, *Nominate* | Put a name forward. | *leaders* and voters, while nominations are open |
| *Remove* on a voter | Takes the person off the roll. | *leaders*, while nominations are open |
| *Add a voter* and *Add voter* | Put a person on the roll. | *leaders*, while nominations are open |
| The scores and *Submit ballot* | 1 score from 0 to 5 per candidate. You can change the ballot until voting closes. | voters, while voting is open |
| *Start new round* | Opens the dialog *Start a new round* with the 2 dates and *Start round*. Candidates and the roll carry over; the ballots do not. | *leaders*, under *Awaiting decision* |

The sections, from the top:

- A card with the note on the Ignatian election: pray, vote, then debate, and repeat as needed. The vote is consultative; the pastor appoints.
- *Candidates*: 1 card per candidate. The card shows the name (a link to the profile), the badges *Appointed* and *STAR winner*, and the workload badge. The workload badge shows to *admin* and to the leaders of the candidate's own teams. Under them: *nominated by* an address, the nomination note, and *Current commitments:* with 1 badge per team and role, or *none*.
- *Voting members*: *N of N ballots cast*, then 1 row per voter. The icon *Ballot cast* marks a voter who has voted. *no account — cannot vote* marks a voter with no account.
- *Your ballot* (voters, while voting is open): the note on STAR voting, 1 row of scores per candidate, *Submit ballot*.
- While voting is open, everyone else sees *Voting is in progress. The tally appears once voting closes.*
- *Result* (after voting closes): *N ballots cast*, *STAR winner:* a name, or *Tie between* names. Then 1 row per candidate with *N points*, the badge *finalist* and *preferred on N ballots*. The tally is advisory.

A reader who cannot see the election sees *This proposal is visible to its voting members and to the team's managers.*

## Your account

Shown to *everyone*, from *Password & sign-in* in the settings menu. It has 4 cards.

| Card | What is on it |
|---|---|
| Your address | Your email address, and *You sign in with your email address and a password.* or *You sign in with a one-time code emailed to this address.* Without a password, a note says a password is optional. |
| *Your duties in your own calendar* | The button *Add to your calendar*, the same panel as on the *Events* page. |
| *Change your email address* | The field *New email address* and the button *Send confirmation*. While a change waits: *Waiting for … to confirm — the link stops working …* and the button *Cancel*. |
| *Change your password* or *Set a password* | *Current password* (only if you signed in with a password), *New password*, *Repeat new password*, *Save password*. With a password set: *Remove password*. |

- After a save the page says *Password saved. You can sign in with it from now on.*
- *Remove password* asks first. After it, the page says you now sign in with emailed codes.
- If you signed in with an emailed code, a note says you can set a new password without the old one.

## Accounts

Shown to *admin* only. Anyone else sees *Admins only.* The page lists every account, *N accounts*, 1 row each.

| Control | What it does |
|---|---|
| *Create accounts for all volunteers with email* | Asks, then *Create and email invites* makes an account for every active volunteer with an email address and no account, and emails each an invitation. |
| *New account* | Opens the dialog *New account*: *Email (login)*, *Linked volunteer* (*— match by email —* or a name), the switch *Parish admin (full access)*, *Create*. |
| *Invite link for …* | The dialog after a new account or a new link: the link, how long it works, whether the email went out, *Copy*, *Close*. |
| The badge *invite pending* | A click opens *An invite is already out to …* with the date it runs out. The link itself is not kept. |
| The link icon *Change linked volunteer* | Opens *Linked volunteer for …*: a list with *— not linked —* and every volunteer, *Save*. |
| The key icon *Make admin* / *Revoke admin* | Gives or takes the administrator right. |
| The icon *Disable* / *Enable* | Switches the account off or on. A switched-off account cannot sign in. |
| The mail icon *New invite link (resets password)* | Makes a fresh invitation link, emails it, and removes the password. |

Each row shows:

- A shield icon for an administrator, a grey person for everyone else.
- The email address, and under it the linked volunteer's name or *not linked to a volunteer*.
- 1 badge: *disabled*, *invite pending*, *invite expired*, or *email-code sign-in* for an account with no password. An account with a password and no open invitation has no badge.
- *last login* with the date, once the person has signed in.

## Fields

Shown to *admin* only, under the title *Custom fields*. Anyone else sees *Admins only.*

| Control | What it does |
|---|---|
| *New field* | Opens the dialog *New field*: *Label*, *Type*, *Options (one per line)* for a *Choice* field, the switch *Show as a column on the volunteers list*, *Sort position*, *Save*. |
| The pencil icon | Opens the dialog *Edit field*: the same fields, plus the switch *Active*. The type is shown as text and cannot change. |
| The red bin icon | Asks *Delete the field “…”?*, then *Delete* removes it. The values stay in the history. |

- 1 row per field: the label, a badge with the type, and the options of a *Choice* field. The badge *in list* marks a column on the *Volunteers* page; *inactive* marks a hidden field.
- With no fields: *No custom fields defined yet.*
- The types: *Text*, *Number*, *Choice*, *Date*, *Checkbox*, *Integer*, *Decimal*, *Timestamp*, *Timestamp (with zone)*, *Time*, *Duration*, *UUID*. See [Add a custom field](../how-to/add-a-custom-field.md).

## Workload

Shown to *admin* only, under the title *Workload*. Anyone else sees *Admins only.* A volunteer's score is the sum, over their teams, of the team's weight times the number for their role.

| Card | What is on it |
|---|---|
| *Role multipliers* | 1 number per role: *Ministry leader*, *Second-in-command*, *Core team member*, *Member*. |
| *Colour bands* | 1 row per band: *Label*, *Colour*, *badge text N:1* (how well the label reads on the colour), and *up to score*. The last band reads *everything above*. Under them: *Save settings*. |
| *Team workload weights* | 1 number per team. A cleared box is weight 0: the team does not count. Under them: *Save weights*. |

- After a save: *Workload settings saved* or *Updated N team weights*.
- The number of bands cannot change on this page.

## Ministries

The public pages need no sign-in. They open from *View Team Homepages* on the *Teams* page, from *Public page* on a team page, and from a team's QR code.

- The header shows the logo, *Ministries*, and the link *Sign in*.
- The index, *Ministry home pages*, lists every team with a published page, by full name. With none: *No ministry has published a home page yet.*
- A team's page: the link *← All ministries*, the team's name, *Last updated* with the date, and the content of the team's Google Doc.
- A team with no published page shows *Page not found* and a link to the index.

## The manual

This manual opens in a new tab from *Manual* in the settings menu, and from every link under *Guides* on the *Dashboard*. It needs a sign-in.

- The sidebar holds the search box *Search*. The results come from the pages of this manual.
- The sidebar groups the pages: *Tutorials*; *How-to* by who can do it (*everyone*, *team members and voters*, *core members*, *leaders and seconds*, *administrators*); *Reference*; *Explanation*.
- The switch *Are you a developer? Show the technical manual.* adds the technical pages below the guide. Your browser remembers the choice.

## Related pages

- [The roles](roles.md)
- [Words](words.md)
- [Find a volunteer or a team](../how-to/find-a-volunteer-or-a-team.md)
- [See the parish as of a date](../how-to/see-the-parish-as-of-a-date.md)
- [Who can see what](../explanation/who-can-see-what.md)
- Technical detail: [Permissions and pages](../../reference/permissions.md)
