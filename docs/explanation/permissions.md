# The permission model

The canonical rights table is the
[permission matrix](../reference/permissions.md#permission-matrix); this page
explains why it is shaped that way.

## Roles live on memberships, not on accounts

A parishioner is not "an editor" or "a viewer" — they are *the leader of
Liturgy* and *a member of Hospitality* at the same time. So the unit of
authorization is the membership: each (volunteer, team) pair carries exactly
one of the fourfold roles — **leader**, **second**, **core**, **member** —
and rights are computed per team from those roles. The same fourfold role
is simultaneously *data* (who runs this ministry — which is what coverage
and impact reports read) and *access control* (what they may see and do).
Keeping one source for both is the point: there is no way for the org chart
and the permissions to disagree.

The only global bit is the **admin flag**, and it lives on the account
(`app_user`), not the volunteer — being an administrator is an IT property,
not a ministry standing.

## Cascade and its one exception

Team rights flow down the team tree: leading *Liturgy* means managing
*Music Ministry* and *Altar Servers* too, because that matches how parishes
actually delegate. The plain **member** role is the deliberate exception —
membership grants visibility into *your own* team's names only, not its
sub-teams: belonging to a parent ministry shouldn't reveal every sub-team's
roster.

Two design details follow the same "least surprise, least exposure" line:

- **Redaction over denial.** A member sees the roster with contact fields
  as `•••` rather than being locked out of the page — you may know *who*
  serves without harvesting their phone numbers.
- **Workload is leadership-only.** Scores are visible to admins and to
  leaders/seconds of the volunteer's teams — not to core members and not to
  the volunteer themself. It is a planning signal for people who assign
  work, not a scoreboard ([why it exists](workload.md)).

## One enforcement point

`actors.load_actor` reduces an account to a frozen `Actor` (the type lives
in `permissions.py`, a pure leaf): four precomputed team-id sets — managed,
people, full-view, names-view — with the cascade already applied. The
*people* set is the managed set minus task forces, so leading a task force
never grants rights over the members it borrowed. Every service function takes the actor and
asks it questions (`can_manage_team`, `can_view_full_roster`, …); a failed
check is a returned `Err(Forbidden)` — never an exception — which the GUI
renders as a toast and the API maps to 403
([Errors are values](architecture.md#errors-are-values)).

Because GUI pages and API routers call the *same* service functions with
the *same* actor type, there is exactly one implementation of the matrix —
a right cannot exist in the web interface without existing identically in
the API. The `Actor` is also cheap to reason about: it is built once per
request/interaction from the memberships, so a role change takes effect on
the next action with no cached grants to invalidate.

## Accounts enter the model at the edge

No self-signup exists — accounts are created for people and activated
through invite links ([how](../how-to/manage-users.md), and
[why the login works the way it does](auth.md)). An account without a
linked volunteer has no team roles at all; it can browse the directory and
nothing else unless it is an admin.

Creating one is *almost* an admin-only act. The exception is narrow and
deliberate: a team's leader, second or core member may send an
account-creation link to somebody on that team. The people who read a whole
roster are the people who notice that half of it cannot be reached by email,
and routing every such case through a parish admin made the roster's
sign-in badges informative but useless. Core members are included even
though they cannot manage the roster, because noticing is the relevant
faculty here, not editing.

What keeps this from being a hole in the matrix is that the power is
*create-only*, and only where nothing exists to break: it mints a non-admin
account at the volunteer's own address, and refuses outright any account
that carries a password or has ever been signed into. So a leader can start
somebody off, and cannot touch anybody's working credentials — resets stay
with admins. Re-sending a link that expired unused is allowed for the same
reason: an account nobody ever used holds nothing to lose.
