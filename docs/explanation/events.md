# Events and scheduling

## The problem

The database knew *who belongs where* — teams, roles, rosters — but nothing
about *when anyone actually serves*. Scheduling lived in leaders' heads,
paper lists and group chats: who reads at the 10:30 Mass, who staffs the
fundraiser table, who never showed up, and the scramble when someone falls
ill on Saturday night. The events subsystem gives those four things a home:
a schedule (events and slots), a signal (per-event RSVPs), an escape hatch
(open substitution), and a record (derived attendance and hours).

## One event, one team

Every event belongs to **exactly one team** — deliberately, with no
many-to-many escape. Membership machinery already answers every hard
question scheduling raises: who may see the event (the roster-names rule),
who may manage it (the team's leader and second), who may sign up (its
members), and who gets the substitution email (the same members). A
parish-wide occasion — a bazaar, a jubilee — gets its own **task-force
team** first, built with the ordinary team tools; the event then inherits
its audience, its leaders, even its public ministry page, for free. One
extra team is a small price for never re-implementing permissions.

The app now *automates* that pattern (`services/task_force.py`): an event's
manager adds a **collaborating team** from the event page, which creates
the task-force team as a child of the owning team, copies in the union of
both rosters (highest role per person — collaborating leaders co-manage *the
event*, by design; a task-force role grants nothing over the people in it, see
[Roles and permissions](../reference/permissions.md)), and repoints the event at
it. More teams can be added, and
*Sync rosters* re-copies after source teams drift; nothing syncs in the
background. Once the event ends (or is cancelled), an hourly job restores
the owning team and deletes the task-force team — its memberships stay
visible in as-of history, because they were ordinary versioned rows all
along. The grain never changed: one event, one team; the team just got
cheaper to make. (Adding a collaborator to one occurrence of a weekly
series affects that occurrence only; there is no consent step — any event
manager can pull in another team's roster, the parish trust model.)

## Concrete rows, not recurrence rules

"Every Sunday at 10:30" is stored as N ordinary event rows, materialized at
creation by the *repeat weekly until…* helper (capped at a year). There is
no RRULE engine, no virtual occurrences, no exception table. Editing one
Sunday never surprises the others, every occurrence has plain slots and
assignments, and deleting the concept costs nothing. The copy-forward
recombines wall-clock times in the parish's timezone
([`VDB_TIMEZONE`](../reference/configuration.md)), so a 10:30 Mass repeated
across a DST change stays 10:30 — adding `timedelta(weeks=1)` to the
timestamptz would silently shift it an hour.

The rows of one materialization do share a `series_id` — still not an
engine, just an identity: it exists solely so a sign-up can offer "also
sign me up for the later weeks", copying itself onto each future row that
has a slot of the same *name* (slot ids differ per row; the name is the
series-wide identity). Weeks already full, already served, or whose slot
was renamed simply skip. Repeats created before the column existed have no
series id and never see the offer.

### The double-booking warning

Creating an event runs an advisory check: scheduled events sharing a
parish-local calendar day with any occurrence, whose location reads like
the new one (`difflib` similarity — "parish  hall" still hits "Parish
Hall"), come back as a warning the creator can override with *Create
anyway*. The check deliberately looks across **all** teams — a collision
with a ministry you cannot see is exactly the case worth flagging — but
reveals only the when, the location, and the team path (the team directory
is readable by every member anyway); the title is masked outside the
creator's visibility. The API skips the check: scripts know what they are
inserting.

## Slots and the capacity lock

An event carries named **slots** ("Lector ×2", "Greeter — main door");
capacity `NULL` means unlimited, and an event created without slots gets a
single unlimited *Volunteers* slot — so an attendance-style gathering and a
tightly staffed liturgy are the same shape. Sign-ups count occupants before
inserting, and a *counted* limit cannot be backstopped by a partial unique
index the way one-open-per-seat rules are — so `sign_up`/`assign` take the
slot row `FOR UPDATE` first. This is the codebase's first row lock, held
for a single count-and-insert inside one transaction.

## RSVP is a signal; the assignment is the commitment

Volunteers answer *available / not available* (with a short note) per
event. The answer is *only* a signal: managers see the pool sorted
available-first when assigning, and an assigned person who flips to
unavailable gets an amber badge — nothing automatic happens. Three explicit
paths lead off a slot, all self-serve:

- **Substitution request** — the open call: the assignee posts it,
  teammates not already serving that event get one email, and the first to
  claim takes over — the assignment row itself moves to the claimant, in
  the same transaction as the guarded status flip that decides the race. A
  partial unique index allows one open request per assignment, so repeat
  clicks cannot re-mail the team.
- **Hand off** — the direct version: the assignee picks the teammate
  themselves, who takes the slot immediately and is emailed. Any open call
  on the assignment is cancelled with it, and the hand-off lands in the
  audit log with who acted and when — the dialog says so up front.
- **Withdraw** — leaving the slot open: a reason is required, and it is
  emailed to the team's leaders so the gap gets filled deliberately rather
  than discovered on Sunday. (A manager removing somebody else skips the
  reason — that is their own scheduling decision.)

## Attendance is derived

Nobody takes roll call on Sunday morning. Once a non-cancelled event ends,
everyone still assigned counts as attended for the scheduled duration;
leaders record only the *exceptions* (a no-show, adjusted hours) as
overrides on the assignment row. The roster of a past event therefore *is*
the attendance record — which is why roster changes freeze at `ends_at` and
the overrides become the only lever, while managers may still correct a
wrong end time (recomputing the auto hours). A volunteer's service record
(profile page, `GET /api/volunteers/{id}/hours`) is summed from these rows
on every read; nothing is stored. Hours are a *service record*, distinct
from the [workload score](workload.md), which weighs standing commitments,
not attendance.

## Notices ride the nightly digest

The in-app scheduler is the one clock the app owns. Its 04:00 job
[`jobs.event_reminders`](../reference/cli.md) sends each volunteer at most
one email per night: events a manager scheduled them for, plus two staged
reminders — "coming up this week" once the event is 7 parish days out, and
"tomorrow" the morning before. The stages are per-assignment *preferences*
chosen at sign-up (pre-checked; unticking one silences just that stage).
Day-granularity is deliberate: reminders arrive with the one nightly
digest, not at a computed instant. Idempotency is the proposal-digest
pattern, and now literally the same table: a row in `notification` per
(assignment, stage), which a failed send never writes, so the next night
retries. Self sign-ups and substitution claims are stamped at
insert: the person acted themselves, so only manager assignments earn the
"you have been scheduled" notice; an event pending several notices at once
is listed once, under the strongest, stamping every stage it satisfied.
Time-sensitive mail — the substitution call, its claim, a cancellation —
goes out immediately from the GUI after the transaction commits. The API
sends no mail at all (the repo-wide rule); an API-created assignment still
reaches its volunteer through the digest.

## The public calendar

With the `VDB_GCAL_*` settings provisioned
([how-to](../how-to/google-calendar-sync.md)), a 30-minute in-app job
reconciles events one-way onto a public Google Calendar the parish account
owns, and `/events` embeds it. Published: title, time, location,
description — never slots, rosters, or names, and no team path (so
repointing an event to a task-force team causes no calendar churn).
Cancelled events leave the calendar; past ones stay as history. The sync is
the *single writer*: nothing pushes from the create/edit handlers, so an
edit reaches the calendar within half an hour, and every entry it manages
carries a private marker — hand-made calendar entries are never touched.
Change detection is a payload fingerprint stored on the event row
(`google_event_id` / `google_fingerprint`), which keeps an untouched event
free of API calls.

## Not versioned

None of the five tables is system-versioned, per the `proposal`
precedent: workflow data whose lifecycle is self-recorded
(`status`, `created_at`, `cancelled_at`, `resolved_at`), with the audit
listeners logging every write. The `as_of` time machine does not apply;
the events pages are live-only.
