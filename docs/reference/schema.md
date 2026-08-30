# Database schema

- SQLAlchemy models live in `src/volunteerdb/models.py`.
- Alembic migrations (`migrations/versions/`) create and evolve the schema.
  Nothing else does.
- PostgreSQL-specific features in use: native enum types, `JSONB`,
  `TSTZRANGE`, composite and partial-unique indexes, and PL/pgSQL versioning
  triggers.
- Three tables, `volunteer`, `team` and `membership`, are
  **system-versioned**. Every UPDATE or DELETE archives the previous row
  into a `_history` twin table.
- See [History and time travel](../explanation/history.md) for the design.

## Enums

- Every closed set of values is a native PostgreSQL enum type. So a column
  is typed as the Python enum, and a typo fails at assignment rather than at
  flush.
- Each is a `StrEnum` in `models.py`, so `event.status == "cancelled"` still
  reads true.
- To add a value, run `ALTER TYPE … ADD VALUE`.

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

- `team_role`'s display labels are *Ministry leader*, *Second-in-command*,
  *Core team member*, *Member*.
- The app stores custom-field values as JSON scalars in `volunteer.custom`:
  - integers as numbers
  - checkboxes as booleans
  - everything else as canonical strings (ISO 8601 for the temporal types
    and durations, decimal digits as typed, lowercase UUIDs)

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

- Related rows: `membership` (`ON DELETE CASCADE`), and at most one
  `app_user` (`ON DELETE SET NULL`). If you delete a volunteer, the account
  stays, unlinked.
- There are no ORM `relationship()` attributes anywhere in `models.py`. An
  explicit query loads every association. So no read fires a lazy SELECT
  mid-render, and no write cascades somewhere the service that calls did
  not name.

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

## `membership` (versioned)

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `volunteer_id` | integer | FK → volunteer, `ON DELETE CASCADE`, indexed |
| `team_id` | integer | FK → team, `ON DELETE CASCADE`, indexed |
| `role` | team_role | |
| `sys_period` | tstzrange | |

- Unique on `(volunteer_id, team_id)`: a volunteer holds exactly one role
  per team.
- This is the central relationship: who serves where, in what role.

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
| `calendar_token` | varchar(64) | unique; the personal calendar feed's address, `/calendar/mine/<token>.ics`. Kept in **clear** (it has to be re-shown), rotated from the subscribe panel; NULL until first asked for |
| `otp_hash`, `otp_sent_at`, `otp_expires_at`, `otp_attempts` | | active email OTP (argon2 hash) |
| `is_active` | boolean | inactive accounts cannot sign in |
| `last_login_at`, `created_at` | timestamptz | |

- Team-level rights derive from the linked volunteer's memberships.
- The admin flag is on the account.

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

- The app stores values per volunteer in `volunteer.custom`, under `key`.

(app_setting)=
## `app_setting` (not versioned)

| Column | Type | Notes |
|---|---|---|
| `key` | varchar(100) | PK |
| `value` | jsonb | |
| `updated_at` | timestamptz | |

- Holds two keys:
  - `"workload"`: role multipliers and color bands (see
    [The workload model](../explanation/workload.md)).
  - `"gcal"`: `{"calendar_id", "created_at", "verified_at"}`. The public
    Google Calendar that the sync created for itself, and when the sync last
    verified that it is shared. See
    [Publish events to a Google Calendar](../how-to/google-calendar-sync.md).
- A `"planning"` key once stored `{"clergy_team_id": <id>}`. It named the
  team whose members join every new proposal's voting roll.
- That key was deleted. The answer was always the team named **Clergy**, so
  the roll builder resolves that name directly. No id is stored (see
  [Elections by nomination and vote](../explanation/elections.md)).

(proposal)=
## `proposal` (not versioned)

- One attempt to fill a (team, role) seat by nomination and STAR vote (see
  [Elections by nomination and vote](../explanation/elections.md)).
- The phase of an open proposal (nominating, voting, or concluded) derives
  from today's date against the two deadlines. It is never stored.

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

- Partial unique index `uq_proposal_open` on `(team_id, role) WHERE status =
  'open'`: at most one *open* proposal per seat. Decided ones accumulate as
  history, and a fresh round for the same seat stays legal.
- Deliberately not versioned (nor are the three tables below). This is
  workflow data whose lifecycle `status` and `decided_*` already record. The
  membership that an appointment creates *is* versioned.

## `proposal_candidate` (not versioned)

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `proposal_id` | integer | FK → `proposal.id` ON DELETE CASCADE |
| `volunteer_id` | integer | FK → `volunteer.id` ON DELETE CASCADE |
| `note` | text | the nominator's "why them?" pitch |
| `nominated_by` | integer | FK → `app_user.id` ON DELETE SET NULL |
| `created_at` | timestamptz | |

- Unique on `(proposal_id, volunteer_id)`.
- The candidate set freezes when the voting phase begins (the nomination
  deadline passes).

## `proposal_voter` (not versioned)

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `proposal_id` | integer | FK → `proposal.id` ON DELETE CASCADE |
| `volunteer_id` | integer | FK → `volunteer.id` ON DELETE CASCADE, indexed |
| `added_by` | integer | FK → `app_user.id` ON DELETE SET NULL |
| `created_at` | timestamptz | |

- Unique on `(proposal_id, volunteer_id)`.
- What the nightly digest has already told this voter is in
  [`notification`](#notification), not in columns here.
- At creation, the app prefills the roll. It adds the target team's leader,
  second and core members, plus the members of the team named **Clergy** at
  that moment.
- Like the candidate set, the roll freezes when the voting phase begins.
- The `volunteer_id` index serves the per-request "which rolls am I on?"
  lookup in `load_actor`.

## `proposal_ballot` (not versioned)

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `proposal_id` | integer | FK → `proposal.id` ON DELETE CASCADE, indexed (denormalized: one-query tally/turnout) |
| `voter_id` | integer | FK → `proposal_voter.id` ON DELETE CASCADE, **and** composite FK `(proposal_id, voter_id)` → `proposal_voter (proposal_id, id)` |
| `candidate_id` | integer | FK → `proposal_candidate.id` ON DELETE CASCADE, indexed; likewise composite against `proposal_candidate` |
| `score` | smallint | CHECK `BETWEEN 0 AND 5` |
| `updated_at` | timestamptz | ballots are revisable until the voting deadline |

- The two composite foreign keys keep the denormalized `proposal_id` in
  step. A ballot cannot score a candidate from another proposal, or come
  from a voter on another roll, even if a service forgets to check.
- Unique on `(voter_id, candidate_id)`.
- A cast writes a row for **every** candidate (missing form entries become
  explicit 0s), so "has voted" is simply "has any row".
- Ballots are secret. The API and GUI expose only turnout flags and
  post-conclusion aggregates. `score` is listed in `audit.REDACTED_COLUMNS`,
  so values never reach a log line.

(volunteer_photo)=
## `volunteer_photo` (not versioned)

| Column | Type | Notes |
|---|---|---|
| `volunteer_id` | integer | PK, FK → `volunteer.id` ON DELETE CASCADE |
| `image` | bytea | normalized 400×400 JPEG, ≤ 24 KB (its base64 fits an Excel cell) |
| `content_type` | varchar(50) | `image/jpeg` |
| `uploaded_by` | integer | FK → `app_user.id` ON DELETE SET NULL |
| `uploaded_at` | timestamptz | drives the `?v=` cache-buster in photo URLs |

- Deliberately not versioned (like `custom_field_def`). Photos are
  current-state only, so as-of views show the current photo.
- The blob stays out of `volunteer`, which keeps it out of every list query
  and as-of UNION.

## `team_page` (not versioned)

| Column | Type | Notes |
|---|---|---|
| `team_id` | integer | PK, FK → `team.id` ON DELETE CASCADE |
| `html` | text | sanitized doc HTML; a failed refetch keeps the last good value |
| `fetched_at` | timestamptz | last successful fetch |
| `status` | page_status | `pending` / `ok` / `error` |
| `error` | text | why the last fetch failed |

- A cache of the team's public Google Doc (`team.home_doc_url`).
  `jobs.fetch_pages` refreshes it nightly, and the app serves it at
  `/ministries/`.
- A cache of an external document is current-state by nature. There is
  nothing to version.

## `team_page_image` (not versioned)

| Column | Type | Notes |
|---|---|---|
| `team_id` | integer | PK part, FK → `team.id` ON DELETE CASCADE |
| `seq` | integer | PK part; document order, referenced by the rewritten `src` |
| `image` | bytea | as fetched, except stills over 1600 px are downscaled |
| `content_type` | varchar(50) | |

- Local cached copies of the doc's embedded images, served at
  `/ministries/img/<team_id>/<seq>`.
- The export's signed googleusercontent URLs expire, so the app rewrites
  `team_page.html` to point at these.
- Every successful fetch replaces the whole set. A failed fetch keeps the
  previous set, with the kept html.

(site_logo)=
## `site_logo` (not versioned)

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK; `CHECK (id = 1)` (`ck_site_logo_singleton`), so an upload upserts the one row |
| `image` | bytea | PNG, scaled to fit 1000 × 1000 and kept under 500 KB by `services.branding.normalize` |
| `content_type` | varchar(50) | default `image/png` |
| `uploaded_by` | integer | FK → `app_user.id` ON DELETE SET NULL |
| `uploaded_at` | timestamptz | also the `/logo` ETag, so a revalidation never reads the blob |

- The parish's own mark, uploaded by an admin
  ([Set the site logo](../how-to/site-logo.md)).
- The app shows it in the app header, above the login box, and on the
  public ministries shell.
- If it is absent, `/logo` serves the shipped placeholder.
- Current-state only, like the two image tables above it.

(team_sheet)=
## `team_sheet` (not versioned)

| Column | Type | Notes |
|---|---|---|
| `team_id` | integer | PK, FK → `team.id` ON DELETE CASCADE |
| `file_id` | varchar(128) | unique; Drive file id — survives renames, and the leader-facing link is derived from it |
| `file_name` | varchar(300) | name the sheet currently has on Drive |
| `last_synced_at` | timestamptz | when the last clean sync finished; not advanced for a team whose sync failed |
| `last_status` | sync_status | outcome of the last sync for this team |
| `last_error` | text | |
| `created_at` | timestamptz | |

- The identity of the team's roster spreadsheet. A leader or second sets it
  (`services.teams.set_roster_sheet`), and `jobs.roster_sync` syncs it. See
  [Sync team rosters with Google Sheets](../how-to/roster-spreadsheets.md).
- A pointer to an external artifact: current-state only.
- Revision `0005` dropped a `requested_file_id`/`requested_import` pair.
  That pair held a pasted link *beside* `file_id` until a nightly run could
  verify it.
- That indirection existed only because the app could not reach Drive. A
  link-shared sheet is readable the moment it is pasted, so the app now
  writes `file_id` directly.

(event)=
## `event` (not versioned)

- One occasion that a team serves: a Mass, a fundraiser shift, a task-force
  work day (see [Events and scheduling](../explanation/events.md)).
- An event is always attached to exactly one team. A parish-wide occasion
  gets its own task-force team first.
- "Past" derives from `ends_at` against the clock. It is never stored.

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

- The last two columns replaced an `event_task_force` table. They live here
  because they are properties of the event, and because `SET NULL` is the
  wanted behaviour.
- The old table's `team_id` cascaded, so a delete of a meta team took the
  event and its attendance record with it. Teardown had to repoint and flush
  in the right order to avoid that.
- CHECK `ck_event_task_force_teams` makes sure a task force is never its own
  owner.
- One team can staff at most one event as a task force, hence the unique.
- Indexes:
  - `ix_event_team_starts (team_id, starts_at)` for team-scoped lists
  - `ix_event_starts_at` for the reminder job's date-window scans
  - partial `ix_event_series (series_id) WHERE series_id IS NOT NULL` for
    the series sign-up sweep
- Not versioned (nor are the tables below): workflow data whose lifecycle
  `status`, `created_at` and `cancelled_at` record.

## `event_slot` (not versioned)

- A named position to fill at an event ("Lector", "Greeter — main door").

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `event_id` | integer | FK → `event.id` ON DELETE CASCADE (covered by the unique) |
| `name` | varchar(100) | unique per event (`uq_event_slot_name`) |
| `capacity` | smallint | nullable; NULL = unlimited, CHECK `>= 1` otherwise |
| `position` | integer | display order |
| `description` | varchar(300) | nullable; an optional line under the name. Never identity — the series copy-forward matches on `name` alone |

- An event created without slots gets one unlimited `Volunteers` slot. That
  rule lets one schema serve both staffed liturgies and attendance-only
  gatherings.

## `event_assignment` (not versioned)

- One volunteer in one slot.
- Attendance is **derived, not entered**. An assignment on a past,
  non-cancelled event counts as attended for the scheduled duration, unless
  a manager recorded an exception in the override columns.

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `slot_id` | integer | FK → `event_slot.id` ON DELETE CASCADE, indexed (capacity counts) |
| `event_id` | integer | FK → `event.id` ON DELETE CASCADE; denormalized like `proposal_ballot.proposal_id`, and constrained the same way — composite FK `(slot_id, event_id)` → `event_slot (id, event_id)` means a row cannot hold a slot from another event |
| `volunteer_id` | integer | FK → `volunteer.id` ON DELETE CASCADE, indexed ("my duties", hours) |
| `kind` | assignment_kind | provenance only, no logic branches on it |
| `assigned_by` | integer | FK → `app_user.id` ON DELETE SET NULL |
| `created_at` | timestamptz | |
| `notify_7d`, `notify_24h` | boolean | not null; `notify_24h` defaults true, `notify_7d` false since `0007` (the week-ahead reminder restated a notice the volunteer already had). Reminder-stage *preferences* chosen at sign-up — settings, not records, which is why they stayed here when the stamps left |
| `attended_override` | boolean | nullable; NULL = auto (attended) |
| `hours_override` | numeric(5,2) | nullable; NULL = auto (scheduled duration), CHECK `>= 0` |

- Which notices have gone out for an assignment is in
  [`notification`](#notification).
- Unique `uq_event_assignment (event_id, volunteer_id)`: one slot per person
  per event.
- Rosters freeze once the event ends. The rows become the attendance record,
  and the overrides are the only post-event lever.

## `event_rsvp` (not versioned)

- One volunteer's availability answer for one event. No row = not answered.
- Managers assign from this pool.
- Not a commitment. The assignment is the commitment.

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `event_id` | integer | FK → `event.id` ON DELETE CASCADE (covered by the unique) |
| `volunteer_id` | integer | FK → `volunteer.id` ON DELETE CASCADE, indexed |
| `available` | boolean | |
| `note` | varchar(200) | nullable ("only until 11") |
| `created_at` | timestamptz | |
| `updated_at` | timestamptz | |

- Unique `uq_event_rsvp (event_id, volunteer_id)`. Answers upsert (the
  ballot-PUT idiom).

## `event_sub_request` (not versioned)

- An assignee's open call for a substitute.
- The app emails teammates when it opens.
- The first to claim takes over: the assignment row itself moves to the
  claimant, and `claimed_by_volunteer_id` records who.

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `assignment_id` | integer | FK → `event_assignment.id` ON DELETE CASCADE, indexed |
| `requested_by` | integer | FK → `app_user.id` ON DELETE SET NULL |
| `note` | varchar(200) | nullable; mailed to teammates, an authenticated audience, so it goes out verbatim |
| `status` | sub_request_status | CHECK — `resolved_at` is NULL exactly while `open` |
| `claimed_by_volunteer_id` | integer | FK → `volunteer.id` ON DELETE SET NULL; CHECK — set only on a `claimed` row (one-directional: the FK may null it later) |
| `created_at` | timestamptz | |
| `resolved_at` | timestamptz | nullable |

- Partial unique index `uq_event_sub_request_open` on `(assignment_id) WHERE
  status = 'open'`: at most one open request per assignment. So repeat
  clicks cannot re-mail the team.
- Claims race through a guarded `UPDATE … WHERE status = 'open'`. The loser
  sees a friendly error.

## `event_task_force_source` (not versioned)

- One team whose roster the app copied into a task force (the owner is
  always a source too). So *Sync rosters* knows what to re-copy.

| Column | Type | Notes |
|---|---|---|
| `event_id` | integer | PK part; FK → `event.id` ON DELETE CASCADE |
| `team_id` | integer | PK part; FK → `team.id` ON DELETE CASCADE |

- The pair is the primary key. It was already unique, and a surrogate `id`
  bought nothing: nothing references a source row.
- The task force itself is two columns on [`event`](#event). There is no
  `event_task_force` table.

(notification)=
## `notification` (not versioned)

- One notice already sent, so a nightly job that runs twice does not mail
  twice.

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `stage` | notification_stage | which notice: `event_scheduled`, `event_week`, `event_day`, `roll_added`, `voting_open` |
| `assignment_id` | integer | nullable; FK → `event_assignment.id` ON DELETE CASCADE |
| `voter_id` | integer | nullable; FK → `proposal_voter.id` ON DELETE CASCADE |
| `sent_at` | timestamptz | |

- Unique on `(assignment_id, stage)` and on `(voter_id, stage)`. That is
  what makes a re-run safe: the jobs insert with `ON CONFLICT DO NOTHING`.
- CHECK `(assignment_id IS NULL) <> (voter_id IS NULL)`: exactly one subject
  per row.
- This table replaced five nullable stamp columns spread over
  `event_assignment` and `proposal_voter`. Every new kind of notice used to
  mean another column and another migration. "Who still needs a notice" had
  to `OR` across all of them, a shape no plain index can serve.
- It is deliberately *not* polymorphic. A single `(entity_type, entity_id)`
  pair would have been shorter. But it would have thrown away what the old
  columns had for free: a delete of the assignment deletes its stamps. Two
  nullable foreign keys keep both the cascade and referential integrity.
- A failed send writes no row, so the next night simply tries again.

(job_run)=
## `job_run` (not versioned)

| Column | Type | Notes |
|---|---|---|
| `job_name` | varchar(100) | PK |
| `last_success_on` | date | nullable; the *parish* date, not UTC |
| `last_attempt_at` | timestamptz | nullable |
| `last_exit_code` | integer | nullable; written, never read by the app — it is for whoever is looking into a job that misbehaved |

- Scheduler bookkeeping for the in-app nightly jobs ([CLI and jobs](cli.md)):
  one row per job.
- The table exists so that a restart cannot skip a night. The scheduler runs
  any job whose `last_success_on` predates the parish today.

(mail_quota)=
## `mail_quota` (not versioned)

| Column | Type | Notes |
|---|---|---|
| `day` | date | PK; the *parish* date (`VDB_TIMEZONE`), not UTC |
| `sent` | integer | NOT NULL DEFAULT 0; messages that actually left that day |

- A counter, not a log: no address, no subject. An upsert from the one
  success path in `services.mail.send_email` bumps it.
- `services.mail_quota` reads it into the admin-only header banner. That
  banner warns before the mail provider's allowance (200 messages a day,
  1,000 a month) runs out. Otherwise the app discovers that only when a
  sign-in code fails to send.
- Infrastructure bookkeeping like `job_run`, and not versioned for the same
  reason.

## History twins and triggers

`volunteer_history`, `team_history`, `membership_history` each hold the live
table's columns **in live order** (without PK/FK/defaults) followed by:

| Column | Type | Notes |
|---|---|---|
| `changed_by` | integer | `app_user.id` from the transaction-local `app.user_id` setting |
| `op` | char(1) | `U` (update) or `D` (delete) |

- Indexes: btree on `id`, GiST on `sys_period`.
- The shared PL/pgSQL function `versioning()` runs BEFORE UPDATE/DELETE on
  each versioned table. It inserts the old row **positionally**
  (`INSERT … SELECT ($1).*, uid, op`).
- That is why a new live column requires a rebuild of the twin. See
  [Write a database migration](../how-to/write-a-migration.md).

## Migration history

- The chain starts at `0001`.
- The squash folded revisions `0001`–`0028` into a single `0001_initial.py`,
  which describes the finished schema. 28 files of accreted history answered
  a question nobody asks: how the schema got here. They did so at the cost
  of the one people do ask: what it is now.
- The reasons that mattered moved into `models.py`, beside the columns they
  explain.
- `0002` drops the public interest pipeline: the `interest` table and
  `team.application_form_url`. They were one feature in two places: the
  Google Form link only ever went out as the confirmation email that a
  submission triggered.
- `team` is system-versioned and `versioning()` archives positionally. So
  the column drop meant a rebuild of `team_history` to the post-drop order,
  and the historical values went with it.
- The two catch-up scripts in `deploy/` still create `interest`, and
  correctly so. They bring a pre-squash database up to `0001`, and `0002`
  drops it from there.

The revisions since, in order:

- `0003` added a `requested_file_id`/`requested_import` pair to
  `team_sheet`. The pair parked a pasted spreadsheet link beside `file_id`
  until the nightly rclone run could check it. `0005` dropped the pair
  again, once link-shared sheets made a pasted link checkable on the spot
  ({ref}`team_sheet <team_sheet>`).
- `0004` added `event_slot.description`, so a slot's detail no longer has to
  ride in the name that the weekly copy-forward matches on.
- `0006` created [`site_logo`](#site_logo).
- `0007` created [`mail_quota`](#mail_quota) and flipped the default of
  `event_assignment.notify_7d` to false. The 7-day reminder restated a
  notice the volunteer already had, and was a third of all event mail on a
  weekend roster. Rows that already carry a preference keep it.
- `0008` added `app_user.calendar_token`, the personal calendar feed's
  credential. It is stored in clear because the subscribe panel has to show
  it again.

- `0004`, `0006`, `0007` and `0008` are purely additive, so the previous
  image continues to serve while they apply. `0002` is the one that needed
  an attended deploy ([Deploy and upgrade production](../how-to/deploy.md)).
- The statements in `0001` are a frozen snapshot, written out by hand rather
  than generated from `models.Base.metadata` at run time. A migration that
  imports the application's models no longer describes the schema it created
  once those models change again.
- `tests/test_schema_invariants.py::test_the_migration_builds_the_schema_the_models_describe`
  keeps the two in step. It compares the migrated database against the
  models on every run.

:::{note}
Column *declaration* order in `models.py` follows the order that live
databases physically have. That is not always the logical order:
`AppUser.otp_hash` and the columns after it carry a comment that says so. Two
reasons:

- The `versioning()` trigger archives positionally, so a versioned table's
  order is load-bearing.
- A diff of `pg_dump --schema-only` between a fresh database and an upgraded
  one verified the squash. That diff only means something if the two agree.
:::

### Upgrading a database that predates the squash

`0001` creates a schema. It cannot migrate one. A database that carries one
of the last two pre-squash revisions moves forward with SQL instead:

```sh
pg_dump "$VDB_DATABASE_URL" > backup-before-catchup.sql   # first, always
psql "$VDB_DATABASE_URL" -f deploy/catchup-0022.sql   # only from 0022
psql "$VDB_DATABASE_URL" -f deploy/catchup-0023.sql
uv run alembic stamp 0001
```

- `catchup-0022.sql` is revision `0023` restated as SQL, for a database that
  never received it. The live deployment had not, so this is the path it
  took.
- `catchup-0023.sql` applies `0024`–`0028`.
- Each runs in one transaction and refuses to start against any revision but
  its own.
- The check against `0001` built both paths on scratch databases and diffed
  their schema dumps to nothing. So an upgraded database and a fresh one are
  the same database. That includes the two constraint names and the
  now-unused `pg_trgm` extension, which the script normalises.

What `0024`–`0028` changed, for anyone who reads a database that has been
through them:

| Was | Now |
|---|---|
| `volunteer.updated_at` | gone — `lower(sys_period)` is the last-modified time, and the column was ORM-only, so any Core write left it stale |
| `team.workload_weight` nullable, "NULL counts as 0" | `NOT NULL DEFAULT 0`; the third state had no distinct behaviour |
| `event_task_force_source.id` | dropped for the `(event_id, team_id)` primary key that was already unique |
| lowercase emails by convention | `CHECK (email = lower(email))` on `app_user` and `volunteer` |
| `proposal_ballot.proposal_id` and `event_assignment.event_id` kept in step by the service | composite foreign keys — a ballot's voter and candidate must belong to the proposal it claims, and an assignment's slot to its event |
| no CHECK on `app_user` at all | the invite pair, the email-change triple and the OTP pair are enforced; likewise `event`'s cancellation and `event_sub_request`'s |
| six indexes with no possible consumer | dropped, and ten added for predicates that had none (see the notes in `models.py`) |
| varchar + CHECK for five status columns, nothing for two more | native enum types throughout, and `Mapped[EventStatus]` rather than `Mapped[str]` |
| `event_task_force` table | `event.task_force_team_id` and `event.owner_team_id`, both `ON DELETE SET NULL` — deleting a meta team can no longer take its event's attendance record with it |
| five notification stamp columns across two tables | the `notification` table, keyed by `(subject, stage)` |
