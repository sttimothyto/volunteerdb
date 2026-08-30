# History and time travel

Every team page, volunteer page, and the graph can answer "what did this
look like last September?" The impact and timeline features use the same
records. This page explains the mechanism and its limits.

## System versioning in the database

`volunteer`, `team`, and `membership` are *system-versioned*. Each row
carries a `sys_period`, a `tstzrange` that says "this version was current
from… to…". A PL/pgSQL trigger fires BEFORE UPDATE/DELETE. It archives the
old row version into a twin table (`volunteer_history`, …). Beside it, the
trigger records who changed it (`changed_by`) and what happened (`op`: update
or delete). Live tables hold only current rows; history twins hold every superseded
version.

The use of **triggers rather than application code** is a deliberate choice.
The invariant "no version is ever lost" then holds for *every* write path:
the GUI, the API, a spreadsheet import, an ad-hoc psql session. A code path
that forgot to call an archiver cannot skip it. The app cooperates in exactly
one way. Each transaction sets the Postgres-local `app.user_id`, so the
trigger can stamp `changed_by`. That gives a full audit trail for free.

The cost of the trigger approach is a maintenance rule. The trigger copies
rows *positionally*, so any column change on a versioned table must rebuild
its twin. The recipe is in
[Write a database migration](../how-to/write-a-migration.md).

## As-of reads

A historical query is the union "live rows ∪ history rows whose `sys_period`
contains *t*" (`history.py`). The service layer takes an optional timestamp
and swaps its table references for that union. The *same* roster, graph and
report code serves the present and the past. That is why the GUI's "View as
of" picker (read-only, amber banner) and the API's `?as_of=` parameter
behave identically. They are one mechanism, and [permissions](permissions.md)
apply unchanged to historical data.

The timeline chart and impact math read the history twins directly. They
reconstruct membership spells from the archived versions, with ended and
re-joined spells and mid-spell role promotions included.

## Limits worth knowing

- **Granularity is the transaction**, not the calendar. `sys_period` records
  when the app *wrote* the row. For freshly imported historical data that is
  the import moment, not the real-world join date. (An early revision dropped
  the operator-entered `joined_on`, so timeline spells start at record
  creation.) The seed script backdates its demo history rows precisely
  because real deployments cannot.
- **Not everything is versioned.** Only the roster is: `volunteer`, `team`
  and `membership`. Accounts, custom-field *definitions*, settings, and
  everything under events and elections have no twins. So as-of pages show
  today's field definitions applied to yesterday's data. That is a deliberate
  boundary, not an omission.
  - A versioned table costs a twin table, a rebuild on every column change,
    and double the write volume. That price is worth it for the 3 tables
    whose past anyone asks about, and not for the rest.
  - Events and proposals keep their own record of what happened (the audit
    log, and the decision columns on `proposal`).
  - Likewise [workload](workload.md) as-of scores use historical memberships
    but *today's* multipliers and thresholds. The config is not versioned.
- **History is append-only in practice but not immutable in principle.** A
  superuser can edit twins in SQL. Backups are still the durability story
  ([Back up and restore](../how-to/backup-restore.md)); history is the
  *visibility* story.
- **The trigger archives deletes too** (`op = 'D'`), so "the volunteer we
  removed in March" is still reconstructible as of February.
