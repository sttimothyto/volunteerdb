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
| See capacity scores/bands (all volunteers) | ✓ | | | | |
| View full roster incl. contact details, their teams | ✓ | ✓ | ✓ | | |
| View full volunteer profiles (shared team) | ✓ | ✓ | ✓ | | |
| View roster names (no contact details), own team | ✓ | ✓ | ✓ | ✓ | |
| Browse the team directory | ✓ | ✓ | ✓ | ✓ | ✓ |
| View and edit own profile | ✓ | ✓ | ✓ | ✓ | ✓ |
| Coverage report | ✓ | their teams | | | |
| Create/edit/delete teams | ✓ | | | | |
| Create/delete volunteers; toggle active | ✓ | | | | |
| Parish-wide import/export | ✓ | | | | |
| Accounts, custom fields, capacity config | ✓ | | | | |

Additional rules:

- Volunteers may always view and edit their **own** contact info, whatever
  their roles.
- Capacity is admin-only — deliberately hidden from team leaders, core
  members, *and the volunteer themself*; it is a parish-wide planning
  signal (`Actor.can_view_capacity`).
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
| `/` | Dashboard: quick search, holes-to-fill, my teams | signed in (reports: admin/leaders) |
| `/login` | Password or email-OTP sign-in | public |
| `/invite/{token}` | Redeem invite, optionally set password | public (valid token) |
| `/teams` | Team tree browser | signed in ("New team": admin) |
| `/teams/{id}` | Team detail, roster, as-of picker, roster export | signed in; roster per matrix |
| `/volunteers` | Volunteer list, search; capacity filter for admins | signed in; fields redacted per matrix |
| `/volunteers/{id}` | Profile, timeline, impact report | signed in; detail per matrix |
| `/graph` | Cytoscape ministry graph, as-of picker | signed in |
| `/import` | Spreadsheet import/export | admin or leader/second (scoped to their teams) |
| `/manual` | This documentation (book icon in the header) | signed in |
| `/admin/users` | Accounts: create, invite, bulk provision | admin |
| `/admin/fields` | Custom field definitions | admin |
| `/admin/capacity` | Capacity multipliers, bands, team weights | admin |

The header nav shows Import/Export to admins and team leaders/seconds, and
Accounts, Fields, and Capacity entries to admins only; direct navigation
without the required role is rejected server-side.
