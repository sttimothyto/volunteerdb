# Permissions and pages

- Authorization combines a global **admin flag** on the account with
  per-team **fourfold roles** on memberships: *leader*, *second*, *core*,
  *member*.
- Team roles cascade to sub-teams: the leader of *Liturgy* manages *Music
  Ministry* too. The plain member role applies to the direct team only.
- Enforcement lives in `src/volunteerdb/permissions.py`. The GUI and the API
  share it.
- For the rationale, see
  [The permission model](../explanation/permissions.md).

(permission-matrix)=
## Permission matrix

- "Their teams" always includes sub-teams, except for the member role.

| Capability | admin | leader / second | core | member | any signed-in |
|---|---|---|---|---|---|
| Manage roster (add/remove/change roles), their teams | ✓ | ✓ | | | |
| Edit contact info of volunteers on their teams | ✓ | ✓ | | | |
| Spreadsheet import, their teams | ✓ | ✓ | | | |
| See the team's roster spreadsheet link, their teams | ✓ | ✓ | | | |
| Link a team to a roster spreadsheet, and sync it | ✓ | ✓ | | | |
| See workload scores/bands of volunteers on their teams | ✓ | ✓ | | | |
| View full roster incl. contact details, their teams | ✓ | ✓ | ✓ | | |
| Export their teams' rosters in one file (*Export team(s)*) | ✓ | ✓ | ✓ | | |
| Invite a volunteer on their teams to create an account | ✓ | ✓ | ✓ | | |
| Set the team's public home-page doc, their teams | ✓ | ✓ | ✓ | | |
| View full volunteer profiles (shared team) | ✓ | ✓ | ✓ | | |
| View roster names (no contact details), own team | ✓ | ✓ | ✓ | ✓ | |
| Browse the team directory | ✓ | ✓ | ✓ | ✓ | ✓ |
| View and edit own profile | ✓ | ✓ | ✓ | ✓ | ✓ |
| Coverage report | ✓ | their teams | | | |
| Elections: see vacancies, open proposals, edit deadlines/rolls, appoint, cancel, new round | ✓ | their teams | | | |
| Elections: nominate candidates and vote (STAR) | ✓* | ✓* | ✓* | ✓* | |
| Events: create, edit, cancel, slots, assign, attendance, their teams | ✓ | ✓ | | | |
| Events: view listing/detail, RSVP, sign up, substitutions — own team's events | ✓† | ✓† | ✓† | ✓† | |
| Create/edit/delete teams | ✓ | | | | |
| Create/delete volunteers; toggle active | ✓ | | | | |
| Parish-wide export | ✓ | | | | |
| Accounts, custom fields, workload config | ✓ | | | | |
| Upload, replace or remove the site logo | ✓ | | | | |

Additional rules:

- \* The right to nominate and to vote comes from a seat on a proposal's
  **voting roll** (`proposal_voter`), not from a team role.
  - The roll is prefilled with the target team's leader, second and core
    members, plus the clergy team.
  - Managers can edit the roll while nominations are open.
  - A vote also requires an active account linked to the volunteer.
  - Voters keep read access to their proposals (and tallies) after the
    decision.
- The **Clergy** team is the only team on every proposal's roll.
  - Its members vote on all seats parish-wide.
  - Every other volunteer votes only on seats for the team they lead or hold
    core membership in.
  - Nothing grants the standing but the name. The roll builder looks up the
    team called **Clergy** when it fills a roll.
  - So if you create, rename, or delete that team, you confer or retire the
    standing for future rolls.
  - Managers can still add or drop individual voters on one proposal while
    nominations are open. That edits a roll; it does not move the standing.
- † **Events** follow the roster-names domain for visibility. Anyone who
  can see the owning team's roster names sees an event and its assignee
  names.
  - *Participation* (an RSVP, a sign-up, a claim, or receipt of a
    substitution) also requires real membership of that team, admin or not.
    The service enforces it as a domain invariant.
  - A volunteer can hand their own slot to a chosen teammate, or take
    themselves off it. The app emails the required reason to the team's
    leaders.
  - Managers can hand off or remove anyone's slot.
  - Hand-offs land in the audit log with who acted and when.
  - The creation of an event also runs the advisory cross-team
    double-booking check described in
    [Events and scheduling](../explanation/events.md).
  - When an event gains a **collaborating team**, the auto-created task-force
    team carries copied roles. The collaborating team's leaders and seconds
    become leaders of the task force, and so co-manage the event. The owning
    team's leaders keep control through the team hierarchy.
  - A task force confers rights over the **event only, never over the people
    it borrowed**. Its members do not become yours to read or edit. So
    contact details, notes, workload scores, invites and roster exports all
    stay with the teams they came from.
  - You see who staffs the event, and you manage the staff list. That is
    all.
  - The addition of a collaborator is a unilateral act: the picker offers
    every active team. That is exactly why it cannot carry a right over
    anybody's data.
- Volunteers can always view and edit their **own** contact info, whatever
  their roles.
- The app deliberately hides workload from core members *and from the
  volunteer themself*. It is a leadership planning signal
  (`Actor.can_view_workload`).
  - The score itself is parish-wide, even when the viewer only leads one of
    the volunteer's teams.
- To appoint a candidate (which creates the membership), you need manage
  rights on that team. That is the same rule as a direct roster edit.
  - The STAR tally is advisory: a manager can appoint any candidate.
- Ballots are secret. Nobody ever sees individual scores. The app exposes
  only per-voter turnout flags and, after the voting deadline, aggregates.
- Redaction, not denial: lists and rosters show `•••` for contact fields
  that the viewer cannot see.
  - **Search honours the same line.** The search box and the
    [query language](query-language.md) match names for everyone. They match
    email, phone, notes and custom values only among volunteers that the
    viewer can see unredacted.
  - Otherwise `email LIKE 'j%'` walks an address out of a column the page
    renders as `•••`.
- The team's **public home-page doc** and its **roster spreadsheet** look
  alike. They are gated deliberately differently.
  - The right to set the home-page doc includes core members. Ministry
    leaders here are often elderly, and a public page nobody can refresh
    goes stale. What is at stake is what the page says, under a name the
    parish can correct.
  - The right to link a roster spreadsheet stops at leaders and seconds.
    That sheet carries every member's address, phone and notes, and a link
    hands it a bulk write over the roster.
  - It used to be admin-only. That was not a narrower judgement about
    trust. Nothing in the app could reach Drive, so a pasted link could not
    be checked until the next nightly run.
  - A link-shared sheet is readable the moment it is pasted, and the person
    who runs the ministry can now do this themselves.
- The same rule reaches the **spreadsheet export**. A core member can export
  their team's roster, but the *Volunteer notes* column comes through
  blank. Notes need edit rights everywhere else.
  - The [audit log](../how-to/audit-logs.md) records exports on both
    surfaces.
- Headshots are a deliberate exception to the edit matrix. **Any signed-in
  account** can view, upload, replace, or delete any volunteer's photo
  (panel, detail page, graph, the app bar, and the
  `/api/volunteers/{id}/photo` endpoints).
  - The app does not treat photos as redacted contact detail.
  - The app bar carries your *own* headshot beside your address and opens
    the same upload dialog.
  - An account with no volunteer record behind it shows nothing there. It
    has no row that a photo could hang off.
- The **site logo** is the parish's mark, not a person's photo.
  - Admins upload, replace or remove it: click the logo in the header
    ([Set the site logo](../how-to/site-logo.md)).
  - Everyone sees it. The login page and the public ministries shell fetch
    it from `/logo` with no session at all.
- Your **own email address** is the one field you cannot simply set. It is
  also your login, so it changes only after you open a link mailed to the
  new address.
  - Somebody else's address, edited by an admin or a leader with edit
    rights, applies immediately.
  - See [Authentication design](../explanation/auth.md#changing-the-address).
- **Sign-in status** is likewise not redacted.
  - The profile page shows *Last login* for every volunteer, to every
    viewer, whether or not that viewer can read their contact details.
  - The team roster adds a badge per member that says whether they have a
    VolunteerDB account at all. Everyone who can see the roster's names sees
    the badge.
  - Account state is not system-versioned, so an as-of roster reports who
    can sign in *now*.
- **An invite** is the one account-shaped power that is not admin-only, and
  it hangs off that badge.
  - A viewer with full-roster rights is a leader, second or core member of
    the person's team. For that viewer, the *no account* badge becomes an
    **invite to create account** button on hover or keyboard focus.
  - The button confirms, creates the account and emails the link. The badge
    then reads **invite sent** until the link is redeemed or lapses.
  - A lapsed link can be replaced (*send a new invite*), because an account
    nobody has ever used holds no credential to lose.
  - Everything else about accounts stays admin-only at `/admin/users`. The
    invite cannot disable, promote, relink or reset anybody. It only ever
    mints a non-admin account at the volunteer's own address. The service
    refuses any account that carries a password or has been signed into.
  - No control appears on an as-of snapshot, or for a volunteer with no
    email address on file.
  - The same rule applies over the API: `POST /api/volunteers/{id}/invite`.
- **The invite link itself is shown only to admins.**
  - Whoever holds the link signs in as that volunteer. A leader can add
    anybody to their own team and then correct their address. So a visible
    link turned "invite my team member" into "take over any account that
    has never signed in".
  - For everyone else, the app mails the link to the address on the
    volunteer's own record. It never displays or returns the link.
  - Nobody, admin included, can recover a link already sent. Only its digest
    is stored. So the app **replaces** an outstanding invite rather than
    re-show it, and the previous one stops working.
  - A redirect of somebody else's address also mails the old address, on a
    channel that the editor does not control.

## GUI page index

- The app redirects anonymous browsers to `/login`.
- Only these are exempt:
  - `/login`, `/invite/{token}`, `/confirm-email/{token}`
  - `/api/`
  - the public ministry pages
  - the parish calendar feed
  - the personal feed (its token is the credential)
  - static assets

| Route | Page | Minimum access |
|---|---|---|
| `/` | Dashboard: quick search, statistics, ministry graph; as-of picker | signed in (statistics per the tiers below) |
| `/login` | Password or email-OTP sign-in | public |
| `/invite/{token}` | Redeem invite, optionally set password | public (valid, unexpired token) |
| `/confirm-email/{token}` | Confirm a new email address from the link mailed to it | public (valid, unexpired token) |
| `/ministries/`, `/ministries/{slug}.html`, `/ministries/img/{team}/{seq}` | Public ministry home pages: the index, one page per published team, its cached images | public |
| `/logo` | The site logo (the shipped placeholder until an admin uploads one) | public |
| `/photos/{id}` | A volunteer's headshot | signed in |
| `/docs`, `/openapi.json` | Interactive OpenAPI reference for the JSON API | signed in |
| `/account` | Own sign-in settings: set, change or remove the password (header gear → *Password & sign-in*) | signed in |
| `/teams` | Team hierarchy + coverage counts in one sortable table, search box, as-of picker, *Export team(s)* | signed in (coverage columns: admin/leaders; "New team": admin; export: admin/leader/second/core) |
| `/teams/{id}` | Team detail, roster, as-of picker, roster export, the **Roster spreadsheet** section (link, sync, template, .csv import), invite a member; archive/reactivate the ministry | signed in; roster per matrix; invite needs full-roster rights; archiving is admin-only, like every other team edit |
| `/volunteers` | Volunteer + team search; workload column/filter for admins and leaders/seconds | signed in; fields redacted per matrix |
| `/volunteers/{id}` | Profile, timeline, impact report, invite | signed in; contact details, notes and the impact report per matrix. The **service timeline** and sign-in status are shown to every viewer, like the roster's account badge — who served where, and when, is parish-wide the way the directory and graph already are |
| `/events` | Duties, claimable substitutions, the month calendar (`?view=mine`, the default, or `?view=parish`; `?month=YYYY-MM`) with its subscribe panel, searchable event table; `?team=` narrows to one ministry, `?past=1` shows past and cancelled | signed in; listings and the *mine* view scoped per matrix; the *parish* view lists every team's events but links only those the reader may open ("New event": admin or leader/second) |
| `/calendar/parish.ics` | iCalendar feed of every team's scheduled events — title, time, location, description | public (the public Google calendar carries the same) |
| `/calendar/mine/{token}.ics` | iCalendar feed of the events the account holds a slot at | public path; the token is the credential (`app_user.calendar_token`, rotated from the subscribe panel) |
| `/calendar/mine.ics` | The same, as a downloadable file | signed in |
| `/export/teams.csv` | The parish CSV (admin) or the union of the reader's full-roster teams | signed in; admin, or full-roster rights on at least one team |
| `/teams/{id}/roster.csv` | One team's roster CSV; `?as_of=` for a snapshot | full-roster rights on that team (the exporter checks) |
| `/export/roster-template.csv` | The empty roster template | signed in |
| `/teams/{id}/qr.png` | QR code of the team's public page | signed in; 404 unless the page is published |
| `POST /calendar/mine/reset` | A new personal feed address; the old one stops working | signed in |
| `/events/{id}` | One event: slots (add, rename, re-capacity, remove), sign-up, RSVP, substitutions, attendance | roster-names rights on the owning team |
| `/elections` | Vacancies + the proposal pipeline | admin, leader/second, or voting member of any proposal |
| `/elections/{id}` | One proposal: candidates, roll, ballot form, tally, appoint; deadlines and notes are editable while it is open | managers of that team or its voting members |
| `/manual` | This documentation (header settings gear → *Manual*) | signed in |
| `/admin/users` | Accounts: create, invite, bulk provision | admin |
| `/admin/fields` | Custom field definitions | admin |
| `/admin/workload` | Workload multipliers, bands, team weights | admin |

- The header nav shows *Elections* to admins, team leaders and seconds, and
  anyone on a proposal's voting roll.
- It shows the *Accounts*, *Fields* and *Workload* entries to admins only.
- Spreadsheet import and export have no nav entry of their own. They live on
  `/teams` and on each team's page.
- On narrow screens the nav collapses into a menu button with the same
  entries.
- The server rejects direct navigation without the required role.

### Dashboard statistics

- The dashboard's figures run down the page from the widest audience to the
  narrowest: parish, then leadership, then the reader's own teams and
  service. The ministry graph comes last of all.
- A section that a reader has no right to is *absent*, not empty. The
  queries behind it never run.
- `GET /api/reports/dashboard` gives the same answers over the API.

| Section | Who sees it | What is in it |
|---|---|---|
| Parish | admin | Active volunteers and teams, total assignments, volunteers on no team, how many can sign in |
| Needs attention | admin, or full-roster rights on any team (core/second/leader) | Teams in scope, people on them, people with no email address |
| ⤷ leadership gaps | admin or leader/second, per team | Teams missing a leader or a second, worst first |
| ⤷ workload spread | admin or leader/second, per volunteer | How many people sit in each band |
| ⤷ shifts and open seats | admin or leader/second | Understaffed events in the next 30 days; open proposals by phase |
| My teams | any account linked to a volunteer with at least one membership | Their teams, with their role in each |
| My service | any account linked to a volunteer | Upcoming duties, shifts they could cover, ballots waiting, hours served |

- Two consequences worth a plain statement:
  - A core team member sees the reach of their ministries, but neither the
    coverage gaps nor the workload bands. The teams page and the coverage
    API already draw the same line.
  - Nobody's own workload band appears in *My service*. `can_view_workload`
    excludes the volunteer themself, deliberately.
- Under the as-of picker, the versioned figures answer from the snapshot.
  The dashboard leaves out the live-only ones (shifts, elections, sign-ins),
  with a note that says so.
