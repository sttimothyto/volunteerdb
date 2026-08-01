# Glossary

```{glossary}
volunteer
  A person record: name, contact details, notes, active flag, and
  {term}`custom field` values. May or may not have a login
  ({term}`app user`).

team
  A ministry (e.g. *Liturgy*, *Hospitality*). Teams form a tree: a team may
  have {term}`sub-team`s, and roles cascade down the tree. A team may carry
  a {term}`workload weight`.

sub-team
  A team whose `parent_team_id` points at another team, e.g.
  *Music Ministry* under *Liturgy*. Roles held on the parent cascade to
  sub-teams (except the plain member role).

membership
  The link between one {term}`volunteer` and one {term}`team`, carrying
  exactly one {term}`role`, an optional joined-on date, and notes. A
  volunteer has at most one membership per team.

role
  One of the fourfold per-team roles: {term}`leader`, {term}`second`,
  {term}`core`, {term}`member`. Determines both standing in the ministry and
  access rights ([matrix](permissions.md#permission-matrix)).

leader
  *Ministry leader.* Runs the team: manages the roster of the team and its
  sub-teams and edits contact info of its volunteers.

second
  *Second-in-command.* Same rights as the {term}`leader`; a team with a
  leader but no second still counts as a {term}`vacancy`.

core
  *Core team member.* Sees the full roster including contact details of the
  team and its sub-teams, but does not manage it.

member
  *Member.* Sees roster names (no contact details) of their direct team
  only.

hole
  A leadership gap: a team missing a leader or a second. A volunteer's
  {term}`impact report` is expressed in holes; the open holes parish-wide
  are listed as {term}`vacancy`s on the Planning page.

vacancy
  A currently unfilled {term}`leader` or {term}`second` slot, listed on the
  Planning page (`/planning`), where planners attach {term}`proposal`s.

proposal
  A planner's suggestion to fill a {term}`vacancy`: a volunteer, a role, and
  an optional note, tracked from *proposed* to *accepted*, *declined*, or
  *withdrawn*. Accepting a proposal creates the membership.

impact report
  The answer to "if this volunteer leaves, what holes appear?" — shown on
  the volunteer page and at `GET /api/volunteers/{id}/impact`.

workload weight
  An optional per-team number expressing how demanding serving on that team
  is. Unweighted teams count as 0 in workload scores.

workload
  A volunteer's global workload score: the sum over all their memberships of
  team {term}`workload weight` × role multiplier, bucketed into colored
  {term}`workload band`s. See [The workload model](../explanation/workload.md).

workload band
  A labeled, colored score range (default: green ≤ 4, amber ≤ 8, red above)
  configured on `/admin/workload`.

custom field
  An admin-defined volunteer attribute (text, number, select, date, or
  checkbox) declared on `/admin/fields`; values live in the volunteer's
  JSONB `custom` column.

as-of
  A historical point in time. GUI pages offer a "View as of" date picker and
  most API GETs accept `?as_of=<timestamp>`, reconstructing state from the
  {term}`history twin`s.

history twin
  The `<table>_history` companion of a versioned table (`volunteer`, `team`,
  `membership`), filled by a database trigger with superseded row versions,
  their validity period, and who changed them.

app user
  A login account (`app_user` row), optionally linked to one
  {term}`volunteer`. Carries the admin flag, the password hash (absent for
  OTP-only accounts), and the API token digest.

invite token
  A single-use token embedded in an `/invite/{token}` link. Redeeming it
  activates the account and optionally sets a password.

OTP
  One-time password: the 6-digit code emailed for passwordless sign-in.
  Valid 10 minutes, 5 attempts, resendable after 60 seconds.

API token
  The personal Bearer token for the JSON API, issued by
  `POST /api/auth/login` and stored server-side only as a SHA-256 digest.

seeded data
  The demo dataset created by `scripts/seed.py` — teams, 30 volunteers,
  history spells, and three demo logins — used by the tutorials and tests.
```
