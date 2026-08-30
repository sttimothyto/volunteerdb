# Events and scheduling

## The problem

The database knew *who belongs where* — teams, roles, rosters — but nothing
about *when anyone actually serves*. The schedule lived in leaders' heads,
paper lists and group chats. It held who reads at the 10:30 Mass, who staffs
the fundraiser table, and who never showed up. It also held the scramble
when someone falls ill on Saturday night. The events subsystem gives those 4
things a home. They are a schedule (events and slots), a signal (per-event
RSVPs), an escape hatch (open substitution), and a record (derived
attendance and hours).

## One event, one team

Every event belongs to **exactly one team**, deliberately, with no
many-to-many escape, because the membership machinery already answers every
hard question a schedule raises. It says who can see the event (the
roster-names rule) and who can manage it (the team's leader and second). It
says who can sign up (its members), and who gets the substitution email (the
same members). A parish-wide occasion — a bazaar, a jubilee — gets its own
**task-force team** first, built with the ordinary team tools. The event
then inherits its audience, its leaders, even its public ministry page, for
free. One extra team is a small price for a permission model that nobody
re-implements.

The app now *automates* that pattern (`services/task_force.py`). An event's
manager adds a **collaborator team** from the event page. That creates the
task-force team as a child of the owner team, copies in the union of both
rosters, and repoints the event at it. Each person gets their highest role
across the two rosters, so the leaders of the collaborator team co-manage
*the event*, by design. A task-force role grants nothing over the people in
it; see [Roles and permissions](../reference/permissions.md). The manager
can add more teams, and *Sync rosters* re-copies after the source teams
drift; nothing syncs in the background.

Once the event ends, or a manager cancels it, an hourly job restores the
owner team and deletes the task-force team. Its memberships stay visible in
as-of history, because they were ordinary versioned rows all along. The
grain never changed: one event, one team; the team just got cheaper to make.
(A collaborator added to one occurrence of a weekly series affects that
occurrence only. There is no consent step: any event manager can pull in
another team's roster, which is the parish trust model.)

## Concrete rows, not recurrence rules

The app stores "Every Sunday at 10:30" as N ordinary event rows. The *Repeat
weekly until* helper materializes them at creation (capped at a year). There
is no RRULE engine, no virtual occurrences, no exception table. An edit to
one Sunday never surprises the others, every occurrence has plain slots and
assignments, and a delete of the concept costs nothing. The copy-forward
recombines wall-clock times in the parish's timezone
([`VDB_TIMEZONE`](../reference/configuration.md)). So a 10:30 Mass repeated
across a DST change stays 10:30; `timedelta(weeks=1)` added to the
timestamptz would silently shift it an hour.

The rows of one materialization do share a `series_id`, which is still not
an engine, just an identity. It exists solely so a sign-up can offer "also
sign me up for the later weeks". The sign-up then copies itself onto each
future row that has a slot of the same *name*. Slot ids differ per row; the
name is the series-wide identity. Weeks already full, already served, or
whose slot was renamed simply skip. Repeats created before the column
existed have no series id and never see the offer.

### The double-booking warning

An event creation runs an advisory check. It looks for scheduled events
that share a parish-local calendar day with any occurrence, and whose
location reads like the new one. The match is `difflib` similarity:
"parish  hall" still hits "Parish Hall". Those events come back as a warning
that the creator can override with *Create anyway*.

The check deliberately looks across **all** teams, because a collision with
a ministry you cannot see is exactly the case worth a flag. But it reveals
only the when, the location, and the team path (every member can read the
team directory anyway). It masks the title outside the creator's
visibility. The API skips the check: scripts know what they insert.

## Slots and the capacity lock

An event carries named **slots** ("Lector ×2", "Greeter — main door").
Capacity `NULL` means unlimited, and an event created without slots gets a
single unlimited *Volunteers* slot. So an attendance-style gathering and a
tightly staffed liturgy are the same shape.

A sign-up counts occupants before it inserts. A partial unique index can
backstop a one-open-per-seat rule, but it cannot backstop a *counted*
limit. So `sign_up`/`assign` take the slot row `FOR UPDATE` first. This is
the codebase's first row lock, held for a single count-and-insert inside one
transaction.

A slot can also carry an optional **description**, for example "main door,
from 10:00". It is deliberately not part of the name. The name is the
series-wide identity a copy-forward matches on. A rename to explain a slot
would quietly stop every later week from a match. The description is
decoration on the row, and nothing reads it but the page.

## RSVP is a signal; the assignment is the commitment

Volunteers answer *Available* or *Not available* (with a short note) per
event. The answer is *only* a signal. Managers see the pool sorted
available-first when they assign, and an assigned person who flips to
unavailable gets an amber badge. Nothing automatic happens. Three explicit
paths lead off a slot, all self-serve:

- **Substitution request** — the open call. The assignee posts it,
  teammates who do not already serve at that event get one email, and the
  first to claim takes over. The assignment row itself moves to the
  claimant, in the same transaction as the guarded status flip that decides
  the race. A partial unique index lets one open request exist per
  assignment, so repeat clicks cannot re-mail the team.
  - This is the widest fan-out in the app: one click, a whole roster. So it
    is also the one action with a *volume* limit. A team can broadcast 6
    calls in a rolling day (`SUB_REQUESTS_PER_TEAM_PER_DAY`). After that the
    request is still posted on **/events** for teammates to find; the app
    simply does not announce it, and tells the asker so.
  - The claim notice goes to the asker alone. The team's leaders once got a
    copy too, and were the only recipients with nothing to do.
- **Hand off** — the direct version. The assignee picks the teammate
  themselves, who takes the slot immediately and gets an email. The hand-off
  also cancels any open call on the assignment, and lands in the audit log
  with who acted and when. The dialog says so up front.
- **Withdraw** — the slot stays open. The assignee must give a reason, and
  the team's leaders get it by email. So the leaders fill the gap on
  purpose, instead of a surprise on Sunday. (A manager who removes somebody
  else skips the reason; that is their own decision about the schedule.)

## Attendance is derived

Nobody takes roll call on Sunday morning. Once a non-cancelled event ends,
everyone still assigned counts as attended for the scheduled duration.
Leaders record only the *exceptions* (a no-show, adjusted hours) as
overrides on the assignment row. The roster of a past event therefore *is*
the attendance record. That is why roster changes freeze at `ends_at` and
the overrides become the only lever. Managers can still correct a wrong end
time, which recomputes the auto hours.

The app sums a volunteer's service record (profile page,
`GET /api/volunteers/{id}/hours`) from these rows on every read; it stores
nothing. Hours are a *service record*, distinct from the
[workload score](workload.md), which weighs standing commitments, not
attendance.

## Notices ride the nightly digest

The in-app scheduler is the one clock the app owns. Its 04:00 job
[`jobs.event_reminders`](../reference/cli.md) sends each volunteer at most
one email per night. It covers events a manager scheduled them for, plus
two staged reminders. The first, "coming up this week", goes once the event
is 7 parish days out; the second, "tomorrow", goes the morning before. The
stages are per-assignment *preferences* chosen at sign-up (a tick on one
enables just that stage). **Only "tomorrow" is on by default.**

The week notice restates the "you have been scheduled" message the
volunteer already had, while the 24-hour one is what actually changes their
day. On a weekend roster that middle notice was a third of all event mail
against a 200-a-day allowance ([architecture](architecture.md)). It is
still there for anyone who plans a week out; they just have to ask for it.
Day-granularity is deliberate: reminders arrive with the one nightly digest,
not at a computed instant.

Idempotency is the proposal-digest pattern, and now literally the same
table. A row in `notification` per (assignment, stage) marks a sent notice;
a failed send never writes it, so the next night retries. A self sign-up or
a substitution claim gets its stamp at insert. The person acted themselves,
so only manager assignments earn the "you have been scheduled" notice. The
digest lists an event with several notices due at once only once, under the
strongest, and stamps every stage it satisfied.

Time-sensitive mail — the substitution call, its claim, a cancellation — goes out immediately from
the GUI after the transaction commits. The API sends no mail at all (the
repo-wide rule); an API-created assignment still reaches its volunteer
through the digest.

## The calendar views and feeds

`/events` renders its own month calendar, server-side HTML with no script,
in two views. The first is the reader's duties (the events where they hold a
slot; the default). The second is the whole parish (every team's scheduled
events, linked only where the reader can open them).

Both are also iCalendar feeds (`services/ics.py`, a stdlib RFC 5545
writer): a public parish feed, and a personal feed. The personal feed's
address carries a per-account token, stored in clear on
`app_user.calendar_token`. The app must be able to show it again for a
subscription to be worth it, and it unlocks a duty list, not a sign-in. The
subscribe panel is a native `popover`; a rotation of the address is a plain
form POST, so none of it needs the websocket.

## The public calendar

With the parish Google token provisioned
([how-to](../how-to/google-calendar-sync.md)), a 30-minute in-app job keeps
a public Google Calendar of the parish's events. The calendar is the job's
own creation. The job made it on its first run, named it for the parish,
shared it *anyone can see*, and keeps its id in `app_setting`. Every run
re-checks that share before it writes, and restores the public-reader rule
if missing. The job
reports any rule that would let somebody other than the parish account
write; it does not remove it. That report fails the run, so the alert mail
goes out.

The job publishes the title, time, location and description — never slots,
rosters, or names, and no team path. So an event repointed to a task-force
team causes no calendar churn. Cancelled events leave the calendar; past
ones stay as history. The sync is the *single writer*: nothing pushes from
the create/edit handlers, so an edit reaches the calendar within half an
hour. Every entry it manages carries a private marker; the job logs an
entry somebody typed into the calendar by hand, and leaves it alone. Change
detection is a payload fingerprint stored on the event row
(`google_event_id` / `google_fingerprint`), which keeps an untouched event
free of API calls.
