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
| See workload scores/bands (all volunteers) | ✓ | | | | |
| View full roster incl. contact details, their teams | ✓ | ✓ | ✓ | | |
| View full volunteer profiles (shared team) | ✓ | ✓ | ✓ | | |
| View roster names (no contact details), own team | ✓ | ✓ | ✓ | ✓ | |
| Browse the team directory | ✓ | ✓ | ✓ | ✓ | ✓ |
| View and edit own profile | ✓ | ✓ | ✓ | ✓ | ✓ |
| Coverage report | ✓ | their teams | | | |
| Planning: see vacancies, open proposals, edit deadlines/rolls, appoint, cancel, new round | ✓ | their teams | | | |
| Planning: nominate candidates and vote (STAR) | ✓* | ✓* | ✓* | ✓* | |
| Create/edit/delete teams | ✓ | | | | |
| Create/delete volunteers; toggle active | ✓ | | | | |
| Parish-wide import/export | ✓ | | | | |
| Accounts, custom fields, workload config | ✓ | | | | |

Additional rules:

- \* Nominating and voting are granted by sitting on a proposal's **voting
  roll** (`proposal_voter`), not by team role: the roll is prefilled with
  the target team's leader/second/core plus the configured clergy team, and
  managers may edit it while nominations are open. Voting additionally
  requires an active account linked to the volunteer. Voters keep read
  access to their proposals (and tallies) after the decision.
- Volunteers may always view and edit their **own** contact info, whatever
  their roles.
- Workload is admin-only — deliberately hidden from team leaders, core
  members, *and the volunteer themself*; it is a parish-wide planning
  signal (`Actor.can_view_workload`).
- Appointing a candidate (which creates the membership) requires manage
  rights on that team — the same rule as editing the roster directly. The
  STAR tally is advisory: any candidate may be appointed.
- Ballots are secret. Individual scores are never exposed to anyone —
  only per-voter turnout flags and, once voting concludes, aggregates.
- Redaction, not denial: lists and rosters show `•••` for contact fields the
  viewer may not see.
- Headshots are a deliberate exception to the edit matrix: **any signed-in
  account** may view, upload, replace, or delete any volunteer's photo
  (panel, detail page, graph, and the `/api/volunteers/{id}/photo`
  endpoints). Photos are not treated as redacted contact detail.

## GUI page index

Anonymous browsers are redirected to `/login`; only `/login`,
`/invite/{token}`, `/api/`, and static assets are exempt.

| Route | Page | Minimum access |
|---|---|---|
| `/` | Dashboard: quick search, ministry graph, my teams; as-of picker | signed in |
| `/login` | Password or email-OTP sign-in | public |
| `/invite/{token}` | Redeem invite, optionally set password | public (valid token) |
| `/teams` | Team coverage table + tree browser, as-of picker | signed in (coverage table: admin/leaders; "New team": admin) |
| `/teams/{id}` | Team detail, roster, as-of picker, roster export | signed in; roster per matrix |
| `/volunteers` | Volunteer + team search; workload filter for admins | signed in; fields redacted per matrix |
| `/volunteers/{id}` | Profile, timeline, impact report | signed in; detail per matrix |
| `/planning` | Vacancies + the proposal pipeline; clergy-team setting (admins) | admin, leader/second, or voting member of any proposal |
| `/planning/{id}` | One proposal: candidates, roll, ballot form, tally, appoint | managers of that team or its voting members |
| `/import` | Spreadsheet import/export | admin or leader/second (scoped to their teams) |
| `/manual` | This documentation (book icon in the header) | signed in |
| `/admin/users` | Accounts: create, invite, bulk provision | admin |
| `/admin/fields` | Custom field definitions | admin |
| `/admin/workload` | Workload multipliers, bands, team weights | admin |

The header nav shows Planning to admins, team leaders/seconds, and anyone
sitting on a proposal's voting roll; Import/Export to admins and team
leaders/seconds; and Accounts, Fields, and Workload entries to admins only.
On narrow screens the nav collapses into a menu button with the same
entries. Direct navigation without the required role is rejected
server-side.
