# Glossary

```{glossary}
volunteer
  A person record: name, contact details, notes, active flag, and
  {term}`custom field` values. A volunteer can have a login
  ({term}`app user`), but does not need one.

team
  A ministry, for example *Liturgy* or *Hospitality*. Teams form a tree: a
  team can have {term}`sub-team`s. Roles cascade down the tree. A team can
  carry a {term}`workload weight`.

sub-team
  A team whose `parent_team_id` points at another team, for example
  *Music Ministry* under *Liturgy*. Roles held on the parent cascade to the
  sub-teams, except the plain member role.

membership
  The link between one {term}`volunteer` and one {term}`team`. A membership
  carries exactly one {term}`role`. A volunteer has at most one membership
  per team.

role
  One of the 4 per-team roles: {term}`leader`, {term}`second`,
  {term}`core`, {term}`member`. The role determines both standing in the
  team and access rights ([matrix](permissions.md#permission-matrix)).

leader
  *Ministry leader.* The leader runs the team. The leader manages the roster
  of the team and its sub-teams, and edits the contact details of its
  volunteers.

second
  *Second-in-command.* A second has the same rights as the {term}`leader`.
  A team with a leader but no second still counts as a {term}`vacancy`.

core
  *Core team member.* A core member sees the full roster of the team and
  its sub-teams, with contact details. A core member does not manage the
  roster.

member
  *Member.* A member sees the roster names of their direct team only,
  without contact details.

hole
  A leadership gap: a team without a leader or a second. A volunteer's
  {term}`impact report` counts holes. The *Elections* page lists the open
  holes parish-wide as {term}`vacancy`s.

vacancy
  A {term}`leader` or {term}`second` seat that is unfilled now. The
  *Elections* page (`/elections`) lists the vacancies, and managers open
  {term}`proposal`s there.

proposal
  One attempt to fill a (team, role) seat, with one or more
  {term}`candidate`s, a {term}`voting roll`, and 2 deadlines. Voting members
  can nominate candidates until the *nomination deadline*. The roll then
  scores the candidates by {term}`STAR voting` until the *voting deadline*.
  Finally a manager appoints a candidate, which creates the membership, or
  starts a new round. Statuses: *open* → *appointed* or *cancelled*. The
  phase of an open proposal (nominating / voting / awaiting decision) follows
  from today's date.

candidate
  A volunteer put forward for the seat of a {term}`proposal`. A candidate
  carries the nominator's "why them?" note. On display, a candidate also
  shows their current commitments, the guard against overload of the
  familiar few.

voting roll
  The {term}`voting member`s of one {term}`proposal`. At creation, the roll
  holds the target team's leader, second, and core members, plus the
  {term}`clergy team`. Managers can edit the roll until nominations close.
  After that, the roll freezes.

voting member
  A volunteer on a {term}`voting roll`. A voting member can nominate
  candidates until the nomination deadline. A voting member can cast, and
  revise, a secret STAR ballot until the voting deadline. To cast a ballot,
  the volunteer needs an active linked {term}`app user`.

clergy team
  The parish clergy: the team named **Clergy**. Its members join the
  {term}`voting roll` of every new proposal. It is the *only* team with that
  parish-wide standing. The leadership and core of every other team join a
  roll for their own seat only. Nothing registers the clergy team: the roll
  builder looks the name up when it fills a roll. So a new team with that
  name gets the standing, and a renamed or deleted team loses it, for future
  rolls only.

STAR voting
  *Score Then Automatic Runoff*: each ballot scores every candidate 0–5.
  The 2 candidates with the highest score totals enter an automatic runoff.
  The finalist scored higher on more ballots wins. Similar candidates never
  split the vote, so there is no first-past-the-post spoiler effect. The
  tally reports a tie it cannot resolve; it does not flip a coin. The tally
  is advisory, and the appointment stays a human act.

impact report
  The answer to the question "if this volunteer leaves, what holes appear?"
  The volunteer page shows it, and so does
  `GET /api/volunteers/{id}/impact`.

workload weight
  An optional per-team number that states how demanding service on that
  team is. Unweighted teams count as 0 in workload scores.

workload
  A volunteer's global workload score. It is the sum, over all their
  memberships, of team {term}`workload weight` × role multiplier. Colored
  {term}`workload band`s bucket the score. See
  [The workload model](../explanation/workload.md).

workload band
  A labeled, colored score range (default: green ≤ 4, amber ≤ 8, red
  above). An admin configures the bands on `/admin/workload`.

custom field
  An admin-defined volunteer attribute declared on `/admin/fields`. Its
  type is one of text, number, select, date, checkbox, integer, decimal,
  timestamp, timestamptz, time, interval, or uuid. Values live in the
  volunteer's JSONB `custom` column as JSON scalars.

query filter
  A SQL `WHERE`-clause expression typed into a search box, for example
  `phone LIKE '555%' AND team = 'Liturgy'`. The app compiles it into the
  same permission-checked queries the pages run. See
  [Search query filters](query-language.md).

as-of
  A historical point in time. The dashboard and team pages hide a
  *View as of (YYYY-MM-DD)* date picker in the header's settings menu. Most
  API GETs accept `?as_of=<timestamp>`. The app rebuilds the state from the
  {term}`history twin`s.

history twin
  The `<table>_history` companion of a versioned table (`volunteer`,
  `team`, `membership`). A database trigger fills it with superseded row
  versions, their validity period, and who changed them.

app user
  A login account (`app_user` row). It can link to one {term}`volunteer`.
  It carries the admin flag, the password hash (absent for OTP-only
  accounts), and the API token digest.

invite token
  A single-use, time-limited token embedded in an `/invite/{token}` link
  (7 days by default, `VDB_INVITE_TTL_HOURS`). When the user redeems it,
  the account becomes active, and the user can set a password. A new invite
  token is also the admin-side password reset.

OTP
  One-time password: the 6-digit code sent by email for passwordless
  sign-in. The code is valid for 10 minutes and 5 attempts. The user can
  request a new code after 60 seconds.

API token
  The personal Bearer token for the JSON API, issued by
  `POST /api/auth/login`. The server stores it only as a SHA-256 digest.

password policy
  The rules a password must meet: at least 15 characters, no composition
  rules, and not on the blocklist. The blocklist holds well-known and
  context-specific values (`volunteerdb/passwords.py`, after NIST SP
  800-63B §3.1.1.2). The app enforces the policy wherever a password is
  set. A password never expires.

seeded data
  The demo dataset that `scripts/seed.py` creates, used by the tutorials.
  It holds 34 teams (with a filled {term}`clergy team`), ~150 volunteers,
  history spells, and a schedule either side of today. It also holds a
  proposal in every state and 33 demo logins, all on the password `demo`.
  The tests build their own data.

event
  One occasion a {term}`team` serves: a Mass, a fundraiser shift, a work
  day. An event has a start, an end, a location and any number of
  {term}`slot`s. It belongs to exactly one team. Its status is *scheduled*
  or *cancelled*, and "past" follows from the end time. See
  [Events and scheduling](../explanation/events.md).

slot
  A named position at an {term}`event` (*Lector*, *Greeter*). A slot has a
  capacity and an optional description line, and {term}`assignment`s fill
  it.

assignment
  The link between a volunteer and a {term}`slot`: the commitment, whether
  they signed up themselves or a manager scheduled them. An assignment
  carries the per-sign-up reminder preferences.

RSVP
  A volunteer's signal of whether they can attend an {term}`event`. It is
  a signal only: the {term}`assignment` is the commitment.

substitution request
  A volunteer's request for a replacement on a {term}`slot`. The app mails
  it to the team's members, and any of them can claim it. A cap limits the
  requests per team per day. Status: *open* → *claimed* or *cancelled*.

task force
  The temporary team the app creates when an event's manager adds a
  collaborating team. It is a copy of both rosters. It has rights over the
  {term}`event` only, and never over the people it borrowed. An hourly job
  dismantles it after the event has ended or is cancelled.

attendance
  Who really served. Once the event is past, the app derives attendance
  from the assignments and a manager's overrides. *Hours served* on the
  dashboard counts it.

parish feed
  The public iCalendar feed of every team's scheduled events, at
  `/calendar/parish.ics`. It carries the same events as the public Google
  Calendar.

personal feed
  An account's own duties as an iCalendar feed, at
  `/calendar/mine/<token>.ics`. The {term}`calendar token` in the address
  is the credential.

calendar token
  The last path segment of a {term}`personal feed` address
  (`app_user.calendar_token`). The app creates it the first time the
  account asks for one. The account can rotate it from the subscribe panel
  of the events page. After a rotation, the old address no longer works.

roster spreadsheet
  The Google Sheet a team's roster reconciles with, both ways, every night.
  It is in the [roster CSV format](spreadsheets.md). It is shared "anyone
  with the link can edit". The **Roster spreadsheet** section of the team
  page links to it.

decorated sheet
  A {term}`roster spreadsheet` (or the template) that carries the sync's
  cosmetic guardrails. The guardrails are role and team dropdowns, a hidden
  ID column, and a protected header row. The sync re-applies them every
  night.

link-shared
  Shared as "anyone with the link". Every roster spreadsheet has this share
  setting. The link is the access. There is no per-person list.

site logo
  The parish's own mark. An admin uploads it with a click on the logo in
  the header. The app shows it in the header, above the login box, and on
  the public ministries pages. The app serves it to everyone at `/logo`.

mail allowance
  The mail provider's free tier: 200 messages a day, 1,000 a month. The app
  counts its own sends against it (`mail_quota`). So the header warns an
  admin before a sign-in code fails to send.

Result
  What a fallible service returns instead of an exception: `Ok(value)` or
  `Err(error)` (`fp.py`). The error is a {term}`domain error`. A guarded
  read unwraps with `fp.expect()`.

domain error
  One of the closed set of refusal values in `errors.py`. The set is
  `Forbidden`, `NotFound`, `Invalid`, `Conflict`, `Throttled`, `External`,
  `WeakPassword`, `QueryError`, `BadCredentials`. Each has one user-facing
  message, one HTTP status and one toast.

Env
  The one frozen value that holds everything impure the process needs:
  settings, clock, random source, mailer, HTTP clients, database engine.
  Only a composition root builds it, and a service never sees it.

domain event
  A fact that a service established when it changed data (`SubRequested`,
  `InviteIssued`, …). The service returns it beside its value. It is the
  input to {term}`policy`.

policy
  The pure function from {term}`domain event`s and a context to
  {term}`effect`s (`policy.py`). It decides what to mail, audit or charge,
  from values alone.

effect
  Something the edge does after the commit: `SendMail`, `Audit`,
  `ThrottleHit`. The one interpreter in `effects.py` runs it, so a failed
  send never undoes committed work.

Actor
  A frozen view of the signed-in account, with its team-id sets precomputed
  (managed, people, full-view, names-view). `actors.load_actor` builds it,
  and it answers every permission question ([permissions](permissions.md)).
```
