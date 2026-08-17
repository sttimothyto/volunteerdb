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
one email per night: events a manager scheduled them for, and events they
serve at within the next `VDB_EVENT_REMINDER_DAYS` parish days. Idempotency
is the proposal-digest pattern — nullable per-assignment stamps
(`assigned_notified_at`, `reminder_sent_at`) that a failed send leaves NULL
to retry the next night. Self sign-ups and substitution claims are stamped
at insert: the person acted themselves, so only manager assignments earn
the "you have been scheduled" notice; a newly scheduled event already
inside the reminder window is listed once and stamps both columns.
Time-sensitive mail — the substitution call, its claim, a cancellation —
goes out immediately from the GUI after the transaction commits. The API
sends no mail at all (the repo-wide rule); an API-created assignment still
reaches its volunteer through the digest.

## Not versioned

None of the five tables is system-versioned, per the `proposal`/`interest`
precedent: workflow data whose lifecycle is self-recorded
(`status`, `created_at`, `cancelled_at`, `resolved_at`), with the audit
listeners logging every write. The `as_of` time machine does not apply;
the events pages are live-only.
