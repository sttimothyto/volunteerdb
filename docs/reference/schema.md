# Database schema

SQLAlchemy models live in `src/volunteerdb/models.py`; the schema is created
and evolved exclusively through Alembic migrations (`migrations/versions/`).
PostgreSQL-specific features in use: native enum types, `JSONB`, `TSTZRANGE`,
composite and partial-unique indexes, and PL/pgSQL versioning triggers.

Three tables — `volunteer`, `team`, `membership` — are **system-versioned**:
every UPDATE/DELETE archives the previous row into a `_history` twin table.
See [History and time travel](../explanation/history.md) for the design.

## Enums

Every closed set of values is a native PostgreSQL enum type, so a column is
typed as the Python enum and a typo fails at assignment rather than at flush.
Each is a `StrEnum` in `models.py`, so `event.status == "cancelled"` still reads
true. Adding a value is `ALTER TYPE … ADD VALUE`.

| Type | Values | Column |
|---|---|---|
| `team_role` | `leader` · `second` · `core` · `member` | `membership.role`, `proposal.role` |
| `custom_field_type` | `text` · `number` · `select` · `date` · `checkbox` · `integer` · `decimal` · `timestamp` · `timestamptz` · `time` · `interval` · `uuid` | `custom_field_def.field_type` |
| `proposal_status` | `open` · `appointed` · `cancelled` | `proposal.status` |
| `event_status` | `scheduled` · `cancelled` | `event.status` |
| `assignment_kind` | `signup` · `assigned` · `sub` | `event_assignment.kind` |
| `sub_request_status` | `open` · `claimed` · `cancelled` | `event_sub_request.status` |
| `notification_stage` | `event_scheduled` · `event_week` · `event_day` · `roll_added` · `voting_open` | `notification.stage` |
| `page_status` | `pending` · `ok` · `error` | `team_page.status` |
| `sync_status` | `applied` · `unchanged` · `new` · `error` | `team_sheet.last_status` |

`team_role`'s display labels are *Ministry leader*, *Second-in-command*, *Core
team member*, *Member*.

Custom-field values are stored as JSON scalars in `volunteer.custom`: integers
as numbers, checkboxes as booleans, everything else as canonical strings (ISO
8601 for the temporal types and durations, decimal digits as typed, lowercase
UUIDs).

## `volunteer` (versioned)

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `first_name`, `last_name` | varchar(100) | |
| `email` | varchar(255) | nullable; may be shared by a family; CHECK `= lower(email)`, and indexed on `lower(email)` because every lookup folds case |
| `phone` | varchar(50) | |
| `notes` | text | |
| `is_active` | boolean | |
| `created_at` | timestamptz | no `updated_at`: `lower(sys_period)` is the last-modified time, maintained by the trigger on every write |
| `sys_period` | tstzrange | validity period of this row version |
| `custom` | jsonb | custom-field values keyed by `custom_field_def.key` |

Related rows: `membership` (`ON DELETE CASCADE`), at most one `app_user`
(`ON DELETE SET NULL` — deleting a volunteer leaves the account, unlinked).
There are no ORM `relationship()` attributes anywhere in `models.py`: every
association is loaded by an explicit query, so no read fires a lazy SELECT
mid-render and no write cascades somewhere the calling service did not name.

## `team` (versioned)

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `name` | varchar(200) | unique per parent (`(parent_team_id, name)`, nulls-not-distinct) |
| `parent_team_id` | integer | self-FK, `ON DELETE RESTRICT` — sub-teams protect their parent |
| `description` | text | |
| `is_active` | boolean | |
| `sys_period` | tstzrange | |
| `workload_weight` | numeric(8,2) | NOT NULL DEFAULT 0 — a third state meaning "treat as 0" had no distinct behaviour |
| `home_doc_url` | varchar(500) | nullable; public Google Doc behind the team's `/ministries/` page |
| `application_form_url` | varchar(500) | nullable; the team's Google Form, emailed to public-page interest submitters (prefix-validated to Google Forms) |

## `membership` (versioned)

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `volunteer_id` | integer | FK → volunteer, `ON DELETE CASCADE`, indexed |
| `team_id` | integer | FK → team, `ON DELETE CASCADE`, indexed |
| `role` | team_role | |
| `sys_period` | tstzrange | |

Unique on `(volunteer_id, team_id)`: a volunteer holds exactly one role per
team. This is the central relationship — who serves where, in what role.

## `app_user` (not versioned)

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `volunteer_id` | integer | FK → volunteer, unique, `ON DELETE SET NULL` |
| `email` | varchar(255) | unique login identifier |
| `password_hash` | varchar(255) | argon2; NULL = OTP-only account |
| `is_admin` | boolean | global admin flag |
| `api_token` | varchar(64) | unique; SHA-256 digest of the Bearer token |
| `invite_token` | varchar(64) | unique; pending invite/reset link |
| `invite_expires_at` | timestamptz | set and cleared with `invite_token`; past (or NULL) = the link is dead |
| `pending_email` | varchar(255) | an address waiting on its own confirmation link; deliberately **not** unique — two people may ask, only the first to confirm gets it |
| `email_change_token` | varchar(64) | unique; the link that confirms `pending_email` |
| `email_change_expires_at` | timestamptz | set and cleared with the two above; 24 h (`users.EMAIL_CHANGE_TTL`) |
| `otp_hash`, `otp_sent_at`, `otp_expires_at`, `otp_attempts` | | active email OTP (argon2 hash) |
| `is_active` | boolean | inactive accounts cannot sign in |
| `last_login_at`, `created_at` | timestamptz | |

Team-level rights derive from the linked volunteer's memberships; the admin
flag is on the account.

(custom_field_def)=
## `custom_field_def` (not versioned)

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `key` | varchar(50) | unique slug, immutable after creation |
| `label` | varchar(100) | display name |
| `field_type` | custom_field_type | one of the twelve types above |
| `options` | jsonb | choices for `select` fields |
| `show_in_list` | boolean | promotes the field to a volunteers-table column |
| `position` | integer | display order |
| `is_active` | boolean | |
| `created_at` | timestamptz | |

Values are stored per volunteer in `volunteer.custom` under `key`.

(app_setting)=
## `app_setting` (not versioned)

| Column | Type | Notes |
|---|---|---|
| `key` | varchar(100) | PK |
| `value` | jsonb | |
| `updated_at` | timestamptz | |

Holds one key: `"workload"` — role multipliers and color bands (see
[The workload model](../explanation/workload.md)).

A `"planning"` key once stored `{"clergy_team_id": <id>}`, naming the team
whose members join every new proposal's voting roll. It was deleted: the answer
was always the team named **Clergy**, so the roll builder resolves that name
directly and no id is stored (see
[Elections by nomination and vote](../explanation/elections.md)).

(proposal)=
## `proposal` (not versioned)

One run at filling a (team, role) seat by nomination and STAR vote (see
[Elections by nomination and vote](../explanation/elections.md)). The phase
of an open proposal — nominating, voting, or concluded — derives from
today's date against the two deadlines and is never stored.

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `team_id` | integer | FK → `team.id` ON DELETE CASCADE, indexed |
| `role` | team_role | the same enum `membership.role` uses |
| `status` | proposal_status | |
| `notes` | text | the proposer's framing of the seat |
| `nomination_deadline` | date | last day to nominate, inclusive, in the parish's day (`VDB_TIMEZONE`) |
| `voting_deadline` | date | last day to vote, inclusive; CHECK `nomination_deadline < voting_deadline` |
| `appointed_candidate_id` | integer | FK → `proposal_candidate.id` ON DELETE SET NULL (circular FK, added after both tables); CHECK — set only on an `appointed` row |
| `created_by` | integer | FK → `app_user.id` ON DELETE SET NULL |
| `created_at` | timestamptz | |
| `decided_by` | integer | FK → `app_user.id` ON DELETE SET NULL |
| `decided_at` | timestamptz | CHECK `ck_proposal_decision` — a decided status and `decided_at` stand or fall together |

Partial unique index `uq_proposal_open` on `(team_id, role) WHERE status =
'open'`: at most one *open* proposal per seat, while decided ones
accumulate as history and a fresh round for the same seat stays legal.
Deliberately not versioned (as are the three tables below): workflow data
whose lifecycle is already self-recorded in `status`/`decided_*`; the
membership an appointment creates *is* versioned.

## `proposal_candidate` (not versioned)

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `proposal_id` | integer | FK → `proposal.id` ON DELETE CASCADE |
| `volunteer_id` | integer | FK → `volunteer.id` ON DELETE CASCADE |
| `note` | text | the nominator's "why them?" pitch |
| `nominated_by` | integer | FK → `app_user.id` ON DELETE SET NULL |
| `created_at` | timestamptz | |

Unique on `(proposal_id, volunteer_id)`. The candidate set freezes when
voting begins (the nomination deadline passes).

## `proposal_voter` (not versioned)

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `proposal_id` | integer | FK → `proposal.id` ON DELETE CASCADE |
| `volunteer_id` | integer | FK → `volunteer.id` ON DELETE CASCADE, indexed |
| `added_by` | integer | FK → `app_user.id` ON DELETE SET NULL |
| `created_at` | timestamptz | |

Unique on `(proposal_id, volunteer_id)`. What the nightly digest has already
told this voter is in [`notification`](#notification), not in columns here. The roll is prefilled at creation
(target team's leader/second/core + whichever team is named **Clergy** at
that moment) and, like the candidate set, freezes when voting begins. The `volunteer_id` index
serves the per-request "which rolls am I on?" lookup in `load_actor`.

## `proposal_ballot` (not versioned)

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `proposal_id` | integer | FK → `proposal.id` ON DELETE CASCADE, indexed (denormalized: one-query tally/turnout) |
| `voter_id` | integer | FK → `proposal_voter.id` ON DELETE CASCADE, **and** composite FK `(proposal_id, voter_id)` → `proposal_voter (proposal_id, id)` |
| `candidate_id` | integer | FK → `proposal_candidate.id` ON DELETE CASCADE, indexed; likewise composite against `proposal_candidate` |
| `score` | smallint | CHECK `BETWEEN 0 AND 5` |
| `updated_at` | timestamptz | ballots are revisable until the voting deadline |

The two composite foreign keys are what stops the denormalized
`proposal_id` from drifting: a ballot cannot score a candidate from another
proposal, or be cast by a voter on another roll, even if a service forgets to
check. Unique on `(voter_id, candidate_id)`. Casting writes a row for **every**
candidate (missing form entries become explicit 0s), so "has voted" is
simply "has any row". Ballots are secret: the API and GUI expose only
turnout flags and post-conclusion aggregates, and `score` is listed in
`audit.REDACTED_COLUMNS` so values never reach a log line.

(volunteer_photo)=
## `volunteer_photo` (not versioned)

| Column | Type | Notes |
|---|---|---|
| `volunteer_id` | integer | PK, FK → `volunteer.id` ON DELETE CASCADE |
| `image` | bytea | normalized 400×400 JPEG, ≤ 24 KB (its base64 fits an Excel cell) |
| `content_type` | varchar(50) | `image/jpeg` |
| `uploaded_by` | integer | FK → `app_user.id` ON DELETE SET NULL |
| `uploaded_at` | timestamptz | drives the `?v=` cache-buster in photo URLs |

Deliberately not versioned (like `custom_field_def`): photos are
current-state only, so as-of views show the current photo. Keeping the blob
out of `volunteer` also keeps it out of every list query and as-of UNION.

## `team_page` (not versioned)

| Column | Type | Notes |
|---|---|---|
| `team_id` | integer | PK, FK → `team.id` ON DELETE CASCADE |
| `html` | text | sanitized doc HTML; a failed refetch keeps the last good value |
| `fetched_at` | timestamptz | last successful fetch |
| `status` | page_status | `pending` / `ok` / `error` |
| `error` | text | why the last fetch failed |

Cache of the team's public Google Doc (`team.home_doc_url`), refreshed
nightly by `jobs.fetch_pages` and served at `/ministries/`. A cache of an
external document is current-state by nature — nothing to version.

## `team_page_image` (not versioned)

| Column | Type | Notes |
|---|---|---|
| `team_id` | integer | PK part, FK → `team.id` ON DELETE CASCADE |
| `seq` | integer | PK part; document order, referenced by the rewritten `src` |
| `image` | bytea | as fetched, except stills over 1600 px are downscaled |
| `content_type` | varchar(50) | |

Locally cached copies of the doc's embedded images, served at
`/ministries/img/<team_id>/<seq>` (the export's signed googleusercontent
URLs expire, so `team_page.html` is rewritten to these). Replaced wholesale
on every successful fetch; a failed fetch keeps the previous set alongside
the kept html.

## `team_sheet` (not versioned)

| Column | Type | Notes |
|---|---|---|
| `team_id` | integer | PK, FK → `team.id` ON DELETE CASCADE |
| `file_id` | varchar(128) | unique; Drive file id — survives renames, backs the leader-facing sheet link |
| `file_name` | varchar(300) | name the sheet currently has on Drive |
| `last_synced_at` | timestamptz | Drive ModTime recorded after the last upload; sheets not newer than this skip the apply leg |
| `last_status` | sync_status | outcome of the last sync for this team |
| `last_error` | text | |
| `created_at` | timestamptz | |

Identity of the team's roster spreadsheet in Google Drive, maintained by
`jobs.drive_sync` (see the Drive roster sync how-to). A pointer to an
external artifact — current-state only.

## `interest` (not versioned)

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `team_id` | integer | FK → `team.id` ON DELETE CASCADE, indexed |
| `name` | varchar(200) | as typed into the public form |
| `email` | varchar(255) | CHECK `= lower(email)` |
| `phone` | varchar(50) | nullable |
| `note` | text | nullable; shown to managers and mailed to leaders — never echoed to the submitter |
| `created_at` | timestamptz | |
| `resolved_at` | timestamptz | nullable; set when a manager marks it handled |
| `resolved_by` | integer | FK → `app_user.id` ON DELETE SET NULL; CHECK — a resolver implies a `resolved_at` (one-directional, because the FK may null the resolver later) |

One "I'm interested" submission from the team's public `/ministries/` page.
Partial unique index `uq_interest_open` on `(team_id, email) WHERE
resolved_at IS NULL`: at most one open interest per person per team, so
repeat submissions cannot re-mail leaders or the typed address. Not
versioned (like `proposal`): workflow data whose lifecycle is self-recorded
in `created_at`/`resolved_at`.

(event)=
## `event` (not versioned)

One occasion a team serves — a Mass, a fundraiser shift, a task-force work
day (see [Events and scheduling](../explanation/events.md)). Always
attached to exactly one team; a parish-wide occasion gets its own
task-force team first. "Past" derives from `ends_at` against the clock and
is never stored.

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `team_id` | integer | FK → `team.id` ON DELETE CASCADE (covered by `ix_event_team_starts`) |
| `title` | varchar(200) | |
| `description` | text | nullable |
| `location` | varchar(200) | nullable |
| `starts_at` | timestamptz | CHECK `starts_at < ends_at` |
| `ends_at` | timestamptz | |
| `status` | event_status | |
| `cancelled_at` | timestamptz | nullable; CHECK — set exactly when `status = 'cancelled'` |
| `cancelled_by` | integer | FK → `app_user.id` ON DELETE SET NULL |
| `created_by` | integer | FK → `app_user.id` ON DELETE SET NULL |
| `created_at` | timestamptz | |
| `google_event_id` | varchar(1024) | nullable; calendar-sync bookkeeping (`jobs/calendar_sync.py`) — NULL = not on the parish calendar |
| `google_fingerprint` | varchar(64) | nullable; hash of the last-pushed calendar payload (change detection) |
| `series_id` | uuid | nullable; shared by the rows of one weekly materialization, so a sign-up can copy itself forward |
| `task_force_team_id` | integer | nullable, unique; FK → `team.id` **ON DELETE SET NULL** — the auto-created meta team currently staffing this event |
| `owner_team_id` | integer | nullable; FK → `team.id` ON DELETE SET NULL — the team `team_id` is restored to at teardown |

The last two replaced an `event_task_force` table. They live here because they
are properties of the event, and because `SET NULL` is the behaviour that was
wanted: the old table's `team_id` cascaded, so deleting a meta team took the
event and its attendance record with it, and teardown had to repoint-and-flush
in the right order to avoid that. CHECK `ck_event_task_force_teams` keeps a task
force from being its own owner. One team can staff at most one event as a task
force, hence the unique.

Indexes: `ix_event_team_starts (team_id, starts_at)` for team-scoped lists,
`ix_event_starts_at` for the reminder job's date-window scans,
partial `ix_event_series (series_id) WHERE series_id IS NOT NULL` for the
series sign-up sweep. Not versioned (as are the tables below): workflow
data whose lifecycle is self-recorded in
`status`/`created_at`/`cancelled_at`.

## `event_slot` (not versioned)

A named position to fill at an event ("Lector", "Greeter — main door").

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `event_id` | integer | FK → `event.id` ON DELETE CASCADE (covered by the unique) |
| `name` | varchar(100) | unique per event (`uq_event_slot_name`) |
| `capacity` | smallint | nullable; NULL = unlimited, CHECK `>= 1` otherwise |
| `position` | integer | display order |

An event created without slots gets one unlimited `Volunteers` slot — the
rule that lets one schema serve both staffed liturgies and attendance-only
gatherings.

## `event_assignment` (not versioned)

One volunteer filling one slot. Attendance is **derived, not entered**: an
assignment on a past, non-cancelled event counts as attended for the
scheduled duration unless a manager recorded an exception in the two
override columns.

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `slot_id` | integer | FK → `event_slot.id` ON DELETE CASCADE, indexed (capacity counts) |
| `event_id` | integer | FK → `event.id` ON DELETE CASCADE; denormalized like `proposal_ballot.proposal_id`, and constrained the same way — composite FK `(slot_id, event_id)` → `event_slot (id, event_id)` means a row cannot hold a slot from another event |
| `volunteer_id` | integer | FK → `volunteer.id` ON DELETE CASCADE, indexed ("my duties", hours) |
| `kind` | assignment_kind | provenance only, no logic branches on it |
| `assigned_by` | integer | FK → `app_user.id` ON DELETE SET NULL |
| `created_at` | timestamptz | |
| `notify_7d`, `notify_24h` | boolean | not null, default true; reminder-stage *preferences* chosen at sign-up — settings, not records, which is why they stayed here when the stamps left |
| `attended_override` | boolean | nullable; NULL = auto (attended) |
| `hours_override` | numeric(5,2) | nullable; NULL = auto (scheduled duration), CHECK `>= 0` |

Which notices have gone out for an assignment is in
[`notification`](#notification). Unique `uq_event_assignment (event_id,
volunteer_id)`: one slot per person per event. Rosters freeze once the event ends — the rows become the
attendance record; the overrides are the only post-event lever.

## `event_rsvp` (not versioned)

One volunteer's availability answer for one event; no row = not answered.
Managers assign from this pool. Not a commitment — the assignment is.

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `event_id` | integer | FK → `event.id` ON DELETE CASCADE (covered by the unique) |
| `volunteer_id` | integer | FK → `volunteer.id` ON DELETE CASCADE, indexed |
| `available` | boolean | |
| `note` | varchar(200) | nullable ("only until 11") |
| `created_at` | timestamptz | |
| `updated_at` | timestamptz | |

Unique `uq_event_rsvp (event_id, volunteer_id)`; answers upsert (the
ballot-PUT idiom).

## `event_sub_request` (not versioned)

An assignee's open call for a substitute. Teammates are emailed when it
opens; the first to claim takes over — the assignment row itself moves to
the claimant, `claimed_by_volunteer_id` records who.

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `assignment_id` | integer | FK → `event_assignment.id` ON DELETE CASCADE, indexed |
| `requested_by` | integer | FK → `app_user.id` ON DELETE SET NULL |
| `note` | varchar(200) | nullable; mailed to teammates (an authenticated audience, unlike the public interest form) |
| `status` | sub_request_status | CHECK — `resolved_at` is NULL exactly while `open` |
| `claimed_by_volunteer_id` | integer | FK → `volunteer.id` ON DELETE SET NULL; CHECK — set only on a `claimed` row (one-directional: the FK may null it later) |
| `created_at` | timestamptz | |
| `resolved_at` | timestamptz | nullable |

Partial unique index `uq_event_sub_request_open` on `(assignment_id) WHERE
status = 'open'`: at most one open request per assignment, so repeat clicks
cannot re-mail the team. Claims race through a guarded
`UPDATE … WHERE status = 'open'`; the loser sees a friendly error.

## `event_task_force_source` (not versioned)

One team whose roster was copied into a task force (the owner is always a source
too), so *Sync rosters* knows what to re-copy.

| Column | Type | Notes |
|---|---|---|
| `event_id` | integer | PK part; FK → `event.id` ON DELETE CASCADE |
| `team_id` | integer | PK part; FK → `team.id` ON DELETE CASCADE |

The pair is the primary key: it was already unique, and a surrogate `id` bought
nothing — nothing references a source row. The task force itself is two columns
on [`event`](#event); there is no `event_task_force` table.

(notification)=
## `notification` (not versioned)

One notice already sent, so a nightly job that runs twice does not mail twice.

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `stage` | notification_stage | which notice: `event_scheduled`, `event_week`, `event_day`, `roll_added`, `voting_open` |
| `assignment_id` | integer | nullable; FK → `event_assignment.id` ON DELETE CASCADE |
| `voter_id` | integer | nullable; FK → `proposal_voter.id` ON DELETE CASCADE |
| `sent_at` | timestamptz | |

Unique on `(assignment_id, stage)` and on `(voter_id, stage)` — that is what
makes a re-run safe; the jobs insert with `ON CONFLICT DO NOTHING`. CHECK
`(assignment_id IS NULL) <> (voter_id IS NULL)`: exactly one subject per row.

This replaced five nullable stamp columns spread over `event_assignment` and
`proposal_voter`. Every new kind of notice used to mean another column and
another migration, and "who still needs telling" had to `OR` across all of them
— a shape no plain index can serve. It is deliberately *not* polymorphic: a
single `(entity_type, entity_id)` pair would have been shorter and would have
thrown away what the old columns had for free, namely that deleting the
assignment deletes its stamps. Two nullable foreign keys keep the cascade and
referential integrity both.

A failed send writes no row, so the next night simply tries again.

(job_run)=
## `job_run` (not versioned)

| Column | Type | Notes |
|---|---|---|
| `job_name` | varchar(100) | PK |
| `last_success_on` | date | nullable; the *parish* date, not UTC |
| `last_attempt_at` | timestamptz | nullable |
| `last_exit_code` | integer | nullable; written, never read by the app — it is for whoever is looking into a job that misbehaved |

Scheduler bookkeeping for the in-app nightly jobs
([CLI and jobs](cli.md)): one row per job, existing so that a restart cannot
skip a night — the scheduler runs any job whose `last_success_on` predates the
parish today.

## History twins and triggers

`volunteer_history`, `team_history`, `membership_history` each hold the live
table's columns **in live order** (without PK/FK/defaults) followed by:

| Column | Type | Notes |
|---|---|---|
| `changed_by` | integer | `app_user.id` from the transaction-local `app.user_id` setting |
| `op` | char(1) | `U` (update) or `D` (delete) |

Indexes: btree on `id`, GiST on `sys_period`. The shared PL/pgSQL function
`versioning()` runs BEFORE UPDATE/DELETE on each versioned table and inserts
the old row **positionally** (`INSERT … SELECT ($1).*, uid, op`) — which is
why adding a live column requires rebuilding the twin; see
[Write a database migration](../how-to/write-a-migration.md).

## Migration history

There is one revision. Revisions `0001`–`0028` were squashed into a single
`0001_initial.py` describing the finished schema, because twenty-eight files of
accreted history answered a question nobody asks — how the schema got here — at
the cost of the one people do ask: what it is now. The reasoning that was worth
keeping moved into `models.py`, beside the columns it explains.

The statements in that migration are a frozen snapshot, deliberately written out
rather than generated from `models.Base.metadata` at run time: a migration that
imports the application's models stops describing the schema it created the
moment those models change again.
`tests/test_schema_invariants.py::test_the_migration_builds_the_schema_the_models_describe`
is what keeps the two in step, comparing the migrated database against the models
on every run.

:::{note}
Column *declaration* order in `models.py` follows the order live databases
physically have, which is not always the logical order — `AppUser.otp_hash` and
the columns after it carry a comment saying so. Two reasons: the `versioning()`
trigger archives positionally, so a versioned table's order is load-bearing; and
the squash was verified by diffing `pg_dump --schema-only` between a fresh
database and an upgraded one, which only means something if the two agree.
:::

### Upgrading a database that predates the squash

`0001` creates a schema; it cannot migrate one. A database carrying one of the
last two pre-squash revisions is brought forward by SQL instead:

```sh
pg_dump "$VDB_DATABASE_URL" > backup-before-catchup.sql   # first, always
psql "$VDB_DATABASE_URL" -f deploy/catchup-0022.sql   # only from 0022
psql "$VDB_DATABASE_URL" -f deploy/catchup-0023.sql
uv run alembic stamp 0001
```

`catchup-0022.sql` is revision `0023` restated as SQL, for a database that never
received it — which the live deployment had not, so this is the path it actually
took. `catchup-0023.sql` applies `0024`–`0028`. Each runs in one transaction and
refuses to start against any revision but its own. It was verified against `0001` by building both paths on
scratch databases and diffing their schema dumps to nothing, so an upgraded
database and a fresh one are the same database — including the two constraint
names and the now-unused `pg_trgm` extension, which the script normalises.

What `0024`–`0028` changed, for anyone reading a database that has been through
them:

| Was | Now |
|---|---|
| `volunteer.updated_at` | gone — `lower(sys_period)` is the last-modified time, and the column was ORM-only, so any Core write left it stale |
| `team.workload_weight` nullable, "NULL counts as 0" | `NOT NULL DEFAULT 0`; the third state had no distinct behaviour |
| `event_task_force_source.id` | dropped for the `(event_id, team_id)` primary key that was already unique |
| lowercase emails by convention | `CHECK (email = lower(email))` on `interest`, `app_user` and `volunteer` |
| `proposal_ballot.proposal_id` and `event_assignment.event_id` kept in step by the service | composite foreign keys — a ballot's voter and candidate must belong to the proposal it claims, and an assignment's slot to its event |
| no CHECK on `app_user` at all | the invite pair, the email-change triple and the OTP pair are enforced; likewise `interest`'s resolution, `event`'s cancellation and `event_sub_request`'s |
| six indexes with no possible consumer | dropped, and ten added for predicates that had none (see the notes in `models.py`) |
| varchar + CHECK for five status columns, nothing for two more | native enum types throughout, and `Mapped[EventStatus]` rather than `Mapped[str]` |
| `event_task_force` table | `event.task_force_team_id` and `event.owner_team_id`, both `ON DELETE SET NULL` — deleting a meta team can no longer take its event's attendance record with it |
| five notification stamp columns across two tables | the `notification` table, keyed by `(subject, stage)` |
