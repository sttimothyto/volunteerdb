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
| See capacity scores/bands of volunteers on their teams | ✓ | ✓ | | | |
| View full roster incl. contact details, their teams | ✓ | ✓ | ✓ | | |
| View full volunteer profiles (shared team) | ✓ | ✓ | ✓ | | |
| View roster names (no contact details), own team | ✓ | ✓ | ✓ | ✓ | |
| Browse the team directory | ✓ | ✓ | ✓ | ✓ | ✓ |
| View and edit own profile | ✓ | ✓ | ✓ | ✓ | ✓ |
| Coverage report | ✓ | their teams | | | |
| Create/edit/delete teams | ✓ | | | | |
| Create/delete volunteers; toggle active | ✓ | | | | |
| Accounts, custom fields, capacity config, imports | ✓ | | | | |

Additional rules:

- Volunteers may always view and edit their **own** contact info, whatever
  their roles.
- Capacity is deliberately hidden from core members *and from the volunteer
  themself* — it is a leadership planning signal
  (`Actor.can_view_capacity`).
- Redaction, not denial: lists and rosters show `•••` for contact fields the
  viewer may not see.

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
| `/volunteers` | Volunteer list, search, capacity filter | signed in; fields redacted per matrix |
| `/volunteers/{id}` | Profile, timeline, impact report | signed in; detail per matrix |
| `/graph` | Cytoscape ministry graph, as-of picker | signed in |
| `/import` | Spreadsheet import/export | admin |
| `/admin/users` | Accounts: create, invite, bulk provision | admin |
| `/admin/fields` | Custom field definitions | admin |
| `/admin/capacity` | Capacity multipliers, bands, team weights | admin |

The header nav shows Import/Export, Accounts, Fields, and Capacity entries
to admins only; direct navigation by non-admins is rejected server-side.
