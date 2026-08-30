# The permission model

The canonical rights table is the
[permission matrix](../reference/permissions.md#permission-matrix). This page
explains why it has that shape.

## Roles live on memberships, not on accounts

A parishioner is not "an editor" or "a viewer". They are *the leader of
Liturgy* and *a member of Hospitality* at the same time. So the unit of
authorization is the membership. Each (volunteer, team) pair carries exactly
one of the 4 roles: **leader**, **second**, **core**, **member**. The app
computes rights per team from those roles.

The same role is *data* and *access control* at once. As data, it says who
runs this ministry, which is what the coverage and impact reports read. As
access control, it says what that person can see and do. One source for both
is the point: the org chart and the permissions cannot disagree.

The only global bit is the **admin flag**. It lives on the account
(`app_user`), not on the volunteer. To be an administrator is an IT property,
not a ministry standing.

## Cascade and its one exception

Team rights flow down the team tree. To lead *Liturgy* is to manage *Music
Ministry* and *Altar Servers* too, because that matches how parishes actually
delegate. The plain **member** role is the deliberate exception. Membership
grants visibility into the names of *your own* team only, not its sub-teams.
A place in a parent ministry must not reveal every sub-team's roster.

Two design details follow the same "least surprise, least exposure" line:

- **Redaction over denial.** A member sees the roster with the contact fields
  as `•••`; the page does not lock them out. You can know *who* serves
  without a harvest of their phone numbers.
- **Workload is leadership-only.** Admins and the leaders and seconds of the
  volunteer's teams see the scores. Core members do not, and neither does the
  volunteer. It is a signal for the people who assign work, not a scoreboard
  ([why it exists](workload.md)).

## One enforcement point

`actors.load_actor` reduces an account to a frozen `Actor`. The type lives in
`permissions.py`, a pure leaf. The actor holds 4 precomputed team-id sets —
managed, people, full-view, names-view — with the cascade already applied.
The *people* set is the managed set minus task forces, so the leader of a
task force never gets rights over the members it borrowed.

Every service function takes the actor and asks it questions
(`can_manage_team`, `can_view_full_roster`, …). A failed check is a returned
`Err(Forbidden)`, never an exception. The GUI renders it as a toast and the
API maps it to 403 ([Errors are values](architecture.md#errors-are-values)).

GUI pages and API routers call the *same* service functions with the *same*
actor type. So there is exactly one implementation of the matrix. A right
cannot exist in the web interface without an identical right in the API. The
`Actor` is also cheap to reason about. The app builds it once per request or
interaction from the memberships. So a role change takes effect on the next
action, with no cached grants to invalidate.

## Accounts enter the model at the edge

No self-signup exists. Somebody else creates an account for a person, who
then activates it through an invite link. See
[how](../how-to/manage-users.md), and
[why the login works the way it does](auth.md). An account without a linked
volunteer has no team roles at all. It can browse the directory and nothing
else, unless it is an admin.

To create one is *almost* an admin-only act. The exception is narrow and
deliberate: a team's leader, second or core member can send an
account-creation link to somebody on that team. The people who read a whole
roster are the people who notice that email cannot reach half of it. To route
every such case through a parish admin made the roster's sign-in badges
informative but useless. The exception includes core members even though
they cannot manage the roster. To notice is the relevant faculty here, not to
edit.

This is not a hole in the matrix, because the power is *create-only*, and it
applies only where nothing exists to break. It mints a non-admin account at
the volunteer's own address. It refuses outright any account that carries a
password or that anyone has ever signed into. So a leader can start somebody
off, and cannot touch anybody's credentials that are in use; resets stay with
admins. A leader can also re-send a link that expired unused, for the same
reason: an account nobody ever used holds nothing to lose.
