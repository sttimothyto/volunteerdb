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
  exactly one {term}`role`. A volunteer has at most one membership per
  team.

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
  Planning page (`/planning`), where managers open {term}`proposal`s.

proposal
  One run at filling a (team, role) seat: one or more {term}`candidate`s, a
  {term}`voting roll`, and two deadlines. Candidates may be nominated until
  the *nomination deadline*; the roll then scores them by {term}`STAR
  voting` until the *voting deadline*; finally a manager appoints a
  candidate (creating the membership) or starts a new round. Statuses:
  *open* → *appointed* or *cancelled*. The phase of an open proposal
  (nominating / voting / awaiting decision) derives from today's date.

candidate
  A volunteer put forward for the seat a {term}`proposal` is about,
  together with the nominator's "why them?" note and, on display, their
  current commitments — the guard against overloading the familiar few.

voting roll
  The {term}`voting member`s of one {term}`proposal`. Prefilled at creation
  with the target team's leader, second, and core members plus the
  {term}`clergy team`; managers may edit it until nominations close, after
  which it freezes.

voting member
  A volunteer on a {term}`voting roll`: may nominate candidates until the
  nomination deadline and cast (and revise) a secret STAR ballot until the
  voting deadline. Casting a ballot requires an active linked
  {term}`app user`.

clergy team
  The parish clergy: the team named **Clergy**, whose members join every
  new proposal's {term}`voting roll`. It is the *only* team with that
  parish-wide standing; every other team's leadership and core join a roll
  solely for their own seat. Nothing registers it — the roll builder looks
  the name up when it fills a roll — so creating a team by that name
  confers the standing and renaming or deleting it retires the standing,
  for future rolls only.

STAR voting
  *Score Then Automatic Runoff.* Each ballot scores every candidate 0–5;
  the two highest score totals enter an automatic runoff, won by the
  finalist preferred (scored higher) on more ballots. Similar candidates
  never split the vote, so there is no first-past-the-post spoiler effect.
  Unresolvable ties are reported, not coin-flipped — the tally is advisory
  and the appointment stays a human act.

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
  A historical point in time. The dashboard and team pages hide a "View as
  of" date picker in the header's settings menu, and most API GETs accept
  `?as_of=<timestamp>`, reconstructing state from the {term}`history twin`s.

history twin
  The `<table>_history` companion of a versioned table (`volunteer`, `team`,
  `membership`), filled by a database trigger with superseded row versions,
  their validity period, and who changed them.

app user
  A login account (`app_user` row), optionally linked to one
  {term}`volunteer`. Carries the admin flag, the password hash (absent for
  OTP-only accounts), and the API token digest.

invite token
  A single-use, time-limited token embedded in an `/invite/{token}` link
  (7 days by default, `VDB_INVITE_TTL_HOURS`). Redeeming it activates the
  account and optionally sets a password. Re-issuing one is also the
  admin-side password reset.

OTP
  One-time password: the 6-digit code emailed for passwordless sign-in.
  Valid 10 minutes, 5 attempts, resendable after 60 seconds.

API token
  The personal Bearer token for the JSON API, issued by
  `POST /api/auth/login` and stored server-side only as a SHA-256 digest.

password policy
  What a password must be to be accepted: at least 15 characters, no
  composition rules, and not on the blocklist of well-known or
  context-specific values (`volunteerdb/passwords.py`, after NIST SP 800-63B
  §3.1.1.2). Enforced wherever a password is set; never expires.

seeded data
  The demo dataset created by `scripts/seed.py` — 34 teams (including a
  filled {term}`clergy team`), ~150 volunteers, history spells, a schedule
  either side of today, a proposal in every state and 33 demo logins (all on
  the password `demo`) — used by the tutorials. The tests build their own.
```
