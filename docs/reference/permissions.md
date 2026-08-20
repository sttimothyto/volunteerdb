# Permissions and pages

Authorization combines a global **admin flag** on the account with per-team
**fourfold roles** on memberships: *leader*, *second*, *core*, *member*.
Team roles cascade to sub-teams (leader of *Liturgy* manages *Music
Ministry* too); the plain member role applies to the direct team only.
Enforcement lives in `src/volunteerdb/permissions.py` and is shared by the
GUI and the API. For the rationale, see
[The permission model](../explanation/permissions.md).

(permission-matrix)=
## Permission matrix

"Their teams" always includes sub-teams, except for the member role.

| Capability | admin | leader / second | core | member | any signed-in |
|---|---|---|---|---|---|
| Manage roster (add/remove/change roles), their teams | ✓ | ✓ | | | |
| Edit contact info of volunteers on their teams | ✓ | ✓ | | | |
| Spreadsheet import/export, their teams | ✓ | ✓ | | | |
| See the team's Drive roster sheet link, their teams | ✓ | ✓ | | | |
| See workload scores/bands of volunteers on their teams | ✓ | ✓ | | | |
| View full roster incl. contact details, their teams | ✓ | ✓ | ✓ | | |
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
| Point a team at a different Drive roster sheet | ✓ | | | | |
| Create/delete volunteers; toggle active | ✓ | | | | |
| Parish-wide import/export | ✓ | | | | |
| Accounts, custom fields, workload config | ✓ | | | | |

Additional rules:

- \* Nominating and voting are granted by sitting on a proposal's **voting
  roll** (`proposal_voter`), not by team role: the roll is prefilled with
  the target team's leader/second/core plus the clergy team, and managers
  may edit it while nominations are open. Voting additionally requires an
  active account linked to the volunteer. Voters keep read access to their
  proposals (and tallies) after the decision.
- The **Clergy** team is the only team on every proposal's roll. Its
  members vote on all seats parish-wide; every other volunteer votes only
  on seats for the team they lead or hold core membership in. Nothing
  grants the standing but the name: the roll builder looks up the team
  called **Clergy** when it fills a roll, so creating, renaming, or
  deleting that team confers or retires it for future rolls. Managers may
  still add or drop individual voters on one proposal while nominations are
  open; that edits a roll, it does not move the standing.
- † **Events** follow the roster-names domain for visibility: an event and
  its assignee names are shown to anyone who may see the owning team's
  roster names. *Participation* — RSVPs, sign-ups, claiming or receiving a
  substitution — additionally requires real membership of that team, admin
  or not: the service enforces it as a domain invariant. A volunteer may
  hand their own slot to a chosen teammate or take themselves off it (the
  required reason is emailed to the team's leaders); managers may hand off
  or remove anyone's slot. Hand-offs land in the audit log with who acted
  and when. Creating an event also runs the advisory cross-team
  double-booking check described in
  [Events and scheduling](../explanation/events.md). When an event gains a
  **collaborating team**, the auto-created task-force team carries copied
  roles: the collaborating team's leaders/seconds become leaders of the
  task force and so co-manage the event, while the owning team's leaders
  keep control through the team hierarchy. A task force confers rights over
  the **event only, never over the people it borrowed**: its members do not
  become yours to read or edit, so contact details, notes, workload scores,
  invites and roster exports all stay with the teams they came from. You see
  who is staffing, and you manage the staffing; that is all. Adding a
  collaborator is a unilateral act — the picker offers every active team —
  which is exactly why it cannot carry a right over anybody's data.
- Volunteers may always view and edit their **own** contact info, whatever
  their roles.
- Workload is deliberately hidden from core members *and from the volunteer
  themself* — it is a leadership planning signal
  (`Actor.can_view_workload`). The score itself is parish-wide even when
  the viewer only leads one of the volunteer's teams.
- Appointing a candidate (which creates the membership) requires manage
  rights on that team — the same rule as editing the roster directly. The
  STAR tally is advisory: any candidate may be appointed.
- Ballots are secret. Individual scores are never exposed to anyone —
  only per-voter turnout flags and, once voting concludes, aggregates.
- Redaction, not denial: lists and rosters show `•••` for contact fields the
  viewer may not see — and **search honours the same line**. The search box and
  the [query language](query-language.md) match names for everyone, but email,
  phone, notes and custom values only among volunteers the viewer may see
  unredacted. Otherwise `email LIKE 'j%'` walks an address out of a column the
  page renders as `•••`.
- The team's **public home-page doc** and its **Drive roster sheet** look
  alike and are gated deliberately differently. Setting the home-page doc
  includes core members, because ministry leaders here are often elderly and
  a public page nobody can refresh goes stale; what is at stake is what the
  page says, under a name the parish can correct. Repointing the roster
  sheet is admin-only and stays that way: that sheet carries every member's
  address, phone and notes, and adopting one hands it a bulk write over the
  roster.
- The same rule reaches the **spreadsheet export**: a core member may export
  their team's roster, but the *Volunteer notes* column comes through blank,
  because notes need edit rights everywhere else. Exports are recorded in the
  [audit log](../how-to/audit-logs.md) on both surfaces.
- Headshots are a deliberate exception to the edit matrix: **any signed-in
  account** may view, upload, replace, or delete any volunteer's photo
  (panel, detail page, graph, the app bar, and the
  `/api/volunteers/{id}/photo` endpoints). Photos are not treated as redacted
  contact detail. The app bar carries your *own* headshot beside your address
  and opens the same upload dialog; an account with no volunteer record
  behind it shows nothing there, having no row a photo could hang off.
- Your **own email address** is the one field you may not simply set: it is
  also your login, so it changes only after a link mailed to the new address
  is opened. Somebody else's address, edited by an admin or a leader with
  edit rights, applies immediately. See
  [Authentication design](../explanation/auth.md#changing-the-address).
- **Sign-in status** is likewise not redacted. The profile page shows
  *Last login* for every volunteer, to every viewer, whether or not that
  viewer may read their contact details; the team roster adds a badge per
  member saying whether they have a VolunteerDB account at all, visible to
  everyone who can see the roster's names. Account state is not
  system-versioned, so an as-of roster reports who can sign in *now*.
- **Inviting** is the one account-shaped power that is not admin-only, and it
  hangs off that badge. To a viewer with full-roster rights — leader, second
  or core member of the person's team — the *no account* badge becomes an
  **invite to create account** button on hover or keyboard focus; it confirms,
  creates the account, emails the link, and the badge then reads **invite
  sent** until the link is redeemed or lapses. A lapsed link may be replaced
  (*send a new invite*), because an account nobody has ever used holds no
  credential to lose. Everything else about accounts stays admin-only at
  `/admin/users`: this cannot disable, promote, relink or reset anybody, it
  only ever mints a non-admin account at the volunteer's own address, and the
  service refuses any account that carries a password or has been signed into.
  No control appears on an as-of snapshot, or for a volunteer with no email
  address on file. Same rule over the API:
  `POST /api/volunteers/{id}/invite`.
- **The invite link itself is shown only to admins.** Whoever holds it signs in
  as that volunteer, and a leader may add anybody to their own team and then
  correct their address — so a visible link turned "invite my team member" into
  "take over any account that has never signed in". For everyone else the link
  is mailed to the address on the volunteer's own record and never displayed or
  returned. Nobody, admin included, can recover a link already sent: only its
  digest is stored, so an outstanding invite is **replaced** rather than
  re-shown, and the previous one stops working. Redirecting somebody else's
  address also mails the address being replaced, on a channel the person doing
  the edit does not control.

## GUI page index

Anonymous browsers are redirected to `/login`; only `/login`,
`/invite/{token}`, `/api/`, and static assets are exempt.

| Route | Page | Minimum access |
|---|---|---|
| `/` | Dashboard: quick search, statistics, ministry graph; as-of picker | signed in (statistics per the tiers below) |
| `/login` | Password or email-OTP sign-in | public |
| `/invite/{token}` | Redeem invite, optionally set password | public (valid, unexpired token) |
| `/account` | Own sign-in settings: set, change or remove the password (header gear → *Password & sign-in*) | signed in |
| `/teams` | Team hierarchy + coverage counts in one sortable table, search box, as-of picker | signed in (coverage columns: admin/leaders; "New team": admin) |
| `/teams/{id}` | Team detail, roster, as-of picker, roster export, invite a member; archive/reactivate the ministry | signed in; roster per matrix; invite needs full-roster rights; archiving is admin-only, like every other team edit |
| `/volunteers` | Volunteer + team search; workload column/filter for admins and leaders/seconds | signed in; fields redacted per matrix |
| `/volunteers/{id}` | Profile, timeline, impact report, invite | signed in; contact details, notes and the impact report per matrix. The **service timeline** and sign-in status are shown to every viewer, like the roster's account badge — who served where, and when, is parish-wide the way the directory and graph already are |
| `/events` | Duties, claimable substitutions, searchable event table; `?team=` narrows to one ministry, `?past=1` shows past and cancelled | signed in; listings scoped per matrix ("New event": admin or leader/second) |
| `/events/{id}` | One event: slots (add, rename, re-capacity, remove), sign-up, RSVP, substitutions, attendance | roster-names rights on the owning team |
| `/elections` | Vacancies + the proposal pipeline | admin, leader/second, or voting member of any proposal |
| `/elections/{id}` | One proposal: candidates, roll, ballot form, tally, appoint; deadlines and notes are editable while it is open | managers of that team or its voting members |
| `/import` | Spreadsheet import/export | admin or leader/second (scoped to their teams) |
| `/manual` | This documentation (header settings gear → *Manual*) | signed in |
| `/admin/users` | Accounts: create, invite, bulk provision | admin |
| `/admin/fields` | Custom field definitions | admin |
| `/admin/workload` | Workload multipliers, bands, team weights | admin |

The header nav shows Elections to admins, team leaders/seconds, and anyone
sitting on a proposal's voting roll; Import/Export to admins and team
leaders/seconds; and Accounts, Fields, and Workload entries to admins only.
On narrow screens the nav collapses into a menu button with the same
entries. Direct navigation without the required role is rejected
server-side.

### Dashboard statistics

The dashboard's figures run down the page from the widest audience to the
narrowest — parish, then leadership, then the ministry graph, then the
reader's own service. A section a reader has no right to is *absent*, not
empty: the queries behind it never run. Same answers over the API at
`GET /api/reports/dashboard`.

| Section | Who sees it | What is in it |
|---|---|---|
| Parish | admin | Active volunteers and teams, total assignments, volunteers on no team, how many can sign in |
| Needs attention | admin, or full-roster rights on any team (core/second/leader) | Teams in scope, people on them, people with no email address |
| ⤷ leadership gaps | admin or leader/second, per team | Teams missing a leader or a second, worst first |
| ⤷ workload spread | admin or leader/second, per volunteer | How many people sit in each band |
| ⤷ shifts and open seats | admin or leader/second | Understaffed events in the next 30 days; open proposals by phase |
| My service | any account linked to a volunteer | Upcoming duties, shifts they could cover, ballots waiting, hours served, their teams |

Two consequences worth stating outright. A core team member sees the reach
of their ministries but neither the coverage gaps nor the workload bands —
the same line the teams page and the coverage API already draw. And nobody's
own workload band appears in *My service*: `can_view_workload` excludes the
volunteer themself, deliberately.

Under the as-of picker the versioned figures answer from the snapshot and
the live-only ones (shifts, elections, sign-ins) are left out, with a note
saying so.
