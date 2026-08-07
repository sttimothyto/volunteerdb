# History and time travel

Every team page, volunteer page, and the graph can answer "what did this
look like last September?" — and the impact and timeline features are built
on the same records. This page explains the mechanism and its limits.

## System versioning in the database

`volunteer`, `team`, and `membership` are *system-versioned*: each row
carries a `sys_period` (a `tstzrange` — "this version was current from…
to…"), and a PL/pgSQL trigger fires BEFORE UPDATE/DELETE to archive the
outgoing row version into a twin table (`volunteer_history`, …) together
with who changed it (`changed_by`) and what happened (`op`: update or
delete). Live tables hold only current rows; history twins hold every
superseded version.

Doing this with **triggers rather than application code** is a deliberate
choice: the invariant "no version is ever lost" holds for *every* write
path — GUI, API, spreadsheet import, an ad-hoc psql session — and cannot be
skipped by a code path that forgot to call the archiver. The app cooperates
in exactly one way: each transaction sets the Postgres-local `app.user_id`
so the trigger can stamp `changed_by`, giving a full audit trail for free.

The cost of the trigger approach is a maintenance rule: the trigger copies
rows *positionally*, so any column change on a versioned table must rebuild
its twin — the recipe is in
[Write a database migration](../how-to/write-a-migration.md).

## As-of reads

A historical query is the union "live rows ∪ history rows whose
`sys_period` contains *t*" (`history.py`). The service layer takes an
optional timestamp and swaps its table references for that union — the
*same* roster/graph/report code serves the present and the past. That is
why the GUI's "View as of" picker (read-only, amber banner) and the API's
`?as_of=` parameter behave identically: they are one mechanism, and
[permissions](permissions.md) apply unchanged to historical data.

The timeline chart and impact math read the history twins directly:
membership spells (including ended and re-joined ones, and mid-spell role
promotions) are reconstructed from the archived versions.

## Limits worth knowing

- **Granularity is the transaction**, not the calendar. `sys_period` records
  when the row was *written*, which for freshly imported historical data is
  the import moment, not the real-world join date. (Rev 0011 dropped the
  operator-entered `joined_on`, so timeline spells start at record creation.)
  The seed script backdates its demo history rows precisely because real
  deployments cannot.
- **Not everything is versioned.** Accounts, custom-field *definitions*, and
  settings (`app_user`, `custom_field_def`, `app_setting`) have no twins;
  as-of pages show today's field definitions applied to yesterday's data.
  Likewise [workload](workload.md) as-of scores use historical memberships
  but *today's* multipliers and thresholds — the config isn't versioned.
- **History is append-only in practice but not immutable in principle** — a
  superuser can edit twins in SQL. Backups are still the durability story
  ([Back up and restore](../how-to/backup-restore.md)); history is the
  *visibility* story.
- **Deletes are archived too** (`op = 'D'`), so "the volunteer we removed
  in March" is still reconstructible as of February.
