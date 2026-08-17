# Database schema

SQLAlchemy models live in `src/volunteerdb/models.py`; the schema is created
and evolved exclusively through Alembic migrations (`migrations/versions/`).
PostgreSQL-specific features in use: the `team_role` enum, `JSONB`,
`TSTZRANGE`, and PL/pgSQL versioning triggers.

Three tables — `volunteer`, `team`, `membership` — are **system-versioned**:
every UPDATE/DELETE archives the previous row into a `_history` twin table.
See [History and time travel](../explanation/history.md) for the design.

## Enums

`team_role`
: `leader` · `second` · `core` · `member` (display labels: *Ministry
  leader*, *Second-in-command*, *Core team member*, *Member*).

Custom-field types (CHECK constraint, not a PG enum)
: `text` · `number` · `select` · `date` · `checkbox` · `integer` ·
  `decimal` · `timestamp` · `timestamptz` · `time` · `interval` · `uuid`.
  Values are stored as JSON scalars in `volunteer.custom`: integers as
  numbers, checkboxes as booleans, everything else as canonical strings
  (ISO 8601 for the temporal types and durations, decimal digits as
  typed, lowercase UUIDs).

Event statuses (CHECK constraints, not PG enums)
: `event.status`: `scheduled` · `cancelled` — `event_assignment.kind`:
  `signup` · `assigned` · `sub` — `event_sub_request.status`: `open` ·
  `claimed` · `cancelled`.

## `volunteer` (versioned)

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `first_name`, `last_name` | varchar(100) | |
| `email` | varchar(255) | indexed; may be shared by a family; nullable |
| `phone` | varchar(50) | |
| `notes` | text | |
| `is_active` | boolean | |
| `created_at`, `updated_at` | timestamptz | |
| `sys_period` | tstzrange | validity period of this row version |
| `custom` | jsonb | custom-field values keyed by `custom_field_def.key` |

Relationships: `memberships` (cascade delete-orphan), optional one-to-one
`app_user`.

## `team` (versioned)

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `name` | varchar(200) | unique per parent (`(parent_team_id, name)`, nulls-not-distinct) |
| `parent_team_id` | integer | self-FK, `ON DELETE RESTRICT` — sub-teams protect their parent |
| `description` | text | |
| `is_active` | boolean | |
| `sys_period` | tstzrange | |
| `workload_weight` | numeric(8,2) | nullable; NULL counts as 0 in workload scores |
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
| `field_type` | varchar(20) | CHECK: one of the twelve types above |
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

Revision 0008 also stored a `"planning"` key, `{"clergy_team_id": <id>}`,
naming the team whose members join every new proposal's voting roll.
Revision 0009 deleted it: the answer was always the team named **Clergy**,
so the roll builder resolves that name directly and no id is stored (see
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
| `role` | team_role | reuses the enum from `0001` |
| `status` | varchar(20) | CHECK `open / appointed / cancelled` (string + CHECK, like `custom_field_def.field_type`) |
| `notes` | text | the proposer's framing of the seat |
| `nomination_deadline` | date | last day to nominate, inclusive, in the parish's day (`VDB_TIMEZONE`) |
| `voting_deadline` | date | last day to vote, inclusive; CHECK `nomination_deadline < voting_deadline` |
| `appointed_candidate_id` | integer | FK → `proposal_candidate.id` ON DELETE SET NULL (circular FK, added after both tables) |
| `created_by` | integer | FK → `app_user.id` ON DELETE SET NULL |
| `created_at` | timestamptz | |
| `decided_by` | integer | FK → `app_user.id` ON DELETE SET NULL |
| `decided_at` | timestamptz | |

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
| `added_notified_at` | timestamptz | nullable; when the nightly digest told this voter they were added |
| `voting_notified_at` | timestamptz | nullable; when the nightly digest told this voter voting began |

Unique on `(proposal_id, volunteer_id)`. The roll is prefilled at creation
(target team's leader/second/core + whichever team is named **Clergy** at
that moment) and, like the candidate set, freezes when voting begins. The `volunteer_id` index
serves the per-request "which rolls am I on?" lookup in `load_actor`.

## `proposal_ballot` (not versioned)

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `proposal_id` | integer | FK → `proposal.id` ON DELETE CASCADE, indexed (denormalized: one-query tally/turnout) |
| `voter_id` | integer | FK → `proposal_voter.id` ON DELETE CASCADE |
| `candidate_id` | integer | FK → `proposal_candidate.id` ON DELETE CASCADE, indexed |
| `score` | smallint | CHECK `BETWEEN 0 AND 5` |
| `updated_at` | timestamptz | ballots are revisable until the voting deadline |

Unique on `(voter_id, candidate_id)`. Casting writes a row for **every**
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
| `status` | varchar(20) | `pending` / `ok` / `error` |
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
| `last_status` | varchar(20) | outcome of the last sync for this team |
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
| `email` | varchar(255) | stored lowercased |
| `phone` | varchar(50) | nullable |
| `note` | text | nullable; shown to managers and mailed to leaders — never echoed to the submitter |
| `created_at` | timestamptz | |
| `resolved_at` | timestamptz | nullable; set when a manager marks it handled |
| `resolved_by` | integer | FK → `app_user.id` ON DELETE SET NULL |

One "I'm interested" submission from the team's public `/ministries/` page.
Partial unique index `uq_interest_open` on `(team_id, email) WHERE
resolved_at IS NULL`: at most one open interest per person per team, so
repeat submissions cannot re-mail leaders or the typed address. Not
versioned (like `proposal`): workflow data whose lifecycle is self-recorded
in `created_at`/`resolved_at`.

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
| `status` | varchar(20) | CHECK `scheduled / cancelled` (string + CHECK, like `proposal.status`) |
| `cancelled_at` | timestamptz | nullable |
| `cancelled_by` | integer | FK → `app_user.id` ON DELETE SET NULL |
| `created_by` | integer | FK → `app_user.id` ON DELETE SET NULL |
| `created_at` | timestamptz | |
| `google_event_id` | varchar(1024) | nullable; calendar-sync bookkeeping (`jobs/calendar_sync.py`) — NULL = not on the parish calendar |
| `google_fingerprint` | varchar(64) | nullable; hash of the last-pushed calendar payload (change detection) |
| `series_id` | uuid | nullable; shared by the rows of one weekly materialization, so a sign-up can copy itself forward |

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
| `event_id` | integer | FK → `event.id` ON DELETE CASCADE; denormalized like `proposal_ballot.proposal_id` — the service guarantees slot ∈ event |
| `volunteer_id` | integer | FK → `volunteer.id` ON DELETE CASCADE, indexed ("my duties", hours) |
| `kind` | varchar(20) | CHECK `signup / assigned / sub` — provenance only, no logic branches on it |
| `assigned_by` | integer | FK → `app_user.id` ON DELETE SET NULL |
| `created_at` | timestamptz | |
| `assigned_notified_at` | timestamptz | nullable; nightly-digest stamp — set at insert for self sign-ups and sub claims (the person acted themselves) |
| `notify_7d`, `notify_24h` | boolean | not null, default true; reminder-stage preferences chosen at sign-up |
| `reminded_7d_at`, `reminded_24h_at` | timestamptz | nullable; nightly-digest stamps for the week / day-before reminders |
| `attended_override` | boolean | nullable; NULL = auto (attended) |
| `hours_override` | numeric(5,2) | nullable; NULL = auto (scheduled duration), CHECK `>= 0` |

Unique `uq_event_assignment (event_id, volunteer_id)`: one slot per person
per event. Rosters freeze once the event ends — the rows become the
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
| `status` | varchar(20) | CHECK `open / claimed / cancelled` |
| `claimed_by_volunteer_id` | integer | FK → `volunteer.id` ON DELETE SET NULL |
| `created_at` | timestamptz | |
| `resolved_at` | timestamptz | nullable |

Partial unique index `uq_event_sub_request_open` on `(assignment_id) WHERE
status = 'open'`: at most one open request per assignment, so repeat clicks
cannot re-mail the team. Claims race through a guarded
`UPDATE … WHERE status = 'open'`; the loser sees a friendly error.

## `event_task_force` (not versioned)

Marks an event whose owning team was swapped for an auto-created task-force
team so several teams can staff it (`services/task_force.py`). The meta
team and its memberships are ordinary versioned `team`/`membership` rows —
that is what keeps a torn-down task force visible in as-of history.

| Column | Type | Notes |
|---|---|---|
| `event_id` | integer | PK; FK → `event.id` ON DELETE CASCADE |
| `team_id` | integer | unique; FK → `team.id` ON DELETE CASCADE — the meta team |
| `owner_team_id` | integer | FK → `team.id` ON DELETE CASCADE — restored at teardown *before* the meta team dies (`event.team_id` cascades) |
| `created_by` | integer | FK → `app_user.id` ON DELETE SET NULL |
| `created_at` | timestamptz | |

## `event_task_force_source` (not versioned)

One team whose roster was copied into a task force (the owner is always a
source too). Unique `uq_task_force_source (event_id, team_id)`.

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `event_id` | integer | FK → `event_task_force.event_id` ON DELETE CASCADE |
| `team_id` | integer | FK → `team.id` ON DELETE CASCADE |

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

| Revision | Summary |
|---|---|
| `0001` | Core tables, history twins, `versioning()` trigger function, `team_role` enum |
| `0002` | `custom_field_def`, `app_setting`, `volunteer.custom`, `team.workload_weight`; rebuilds both affected history twins (the recipe's origin) |
| `0003` | OTP login columns on `app_user` |
| `0004` | Nulls all `api_token` values — switch to stored SHA-256 digests (irreversible; existing tokens invalidated) |
| `0005` | Performance indexes: pg_trgm search on volunteer, `membership_history.volunteer_id`, `(last_name, first_name)` |
| `0006` | `volunteer_photo` (not versioned — no twin rebuild) |
| `0007` | `proposal` (not versioned); renames the `app_setting` key `capacity` → `workload` |
| `0008` | Replaces `proposal` with the nomination + STAR-voting pipeline: `proposal`, `proposal_candidate`, `proposal_voter`, `proposal_ballot` (all not versioned; 0007 rows dropped) |
| `0009` | Deletes the stored `planning` app_setting row; the clergy team is resolved by name |
| `0010` | `team.home_doc_url` (rebuilds `team_history`); `team_page` and `team_sheet` (not versioned) |
| `0011` | Drops `membership.joined_on` and `membership.notes` (rebuilds `membership_history`; stored values discarded, downgrade restores the columns as NULL) |
| `0012` | `app_user.invite_expires_at` — invite/reset links stop working after `VDB_INVITE_TTL_HOURS`; outstanding invites are backfilled to 24 h after the upgrade |
| `0013` | `team_page_image` (not versioned) — doc images cached locally, page html rewritten to `/ministries/img/…` |
| `0014` | `app_user.confidentiality_agreed_at` — recorded when the invite's confidentiality notice is accepted |
| `0015` | `team.application_form_url` (rebuilds `team_history`); `interest` (not versioned); `proposal_voter.added_notified_at`/`voting_notified_at` for the nightly digest |
| `0016` | Scheduling subsystem: `event`, `event_slot`, `event_assignment`, `event_rsvp`, `event_sub_request` (all not versioned — no twin rebuilds) |
| `0017` | `job_run` (not versioned) — the in-app scheduler's persisted completion record |
| `0018` | Widens the `custom_field_def.field_type` CHECK to the core PostgreSQL scalar types |
| `0019` | `event.google_event_id` / `event.google_fingerprint` — Google Calendar sync bookkeeping |
| `0020` | `event.series_id` + partial index — weekly repeats share an identity so sign-ups can copy forward |
| `0021` | `event_assignment` reminder stages: `notify_7d`/`notify_24h` prefs + `reminded_7d_at`/`reminded_24h_at` stamps replace `reminder_sent_at` (old stamps migrate into the 7-day column) |
| `0022` | `event_task_force` + `event_task_force_source` (not versioned) — provenance behind automated multi-team events |
