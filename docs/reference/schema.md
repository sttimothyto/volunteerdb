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
: `text` · `number` · `select` · `date` · `checkbox`.

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
| `workload_weight` | numeric(8,2) | nullable; NULL counts as 0 in capacity scores |

## `membership` (versioned)

| Column | Type | Notes |
|---|---|---|
| `id` | integer | PK |
| `volunteer_id` | integer | FK → volunteer, `ON DELETE CASCADE`, indexed |
| `team_id` | integer | FK → team, `ON DELETE CASCADE`, indexed |
| `role` | team_role | |
| `joined_on` | date | optional |
| `notes` | text | |
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
| `invite_token` | varchar(64) | unique; pending invite link |
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
| `field_type` | varchar(20) | CHECK: one of the five types above |
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

Currently holds one key, `"capacity"`: role multipliers and color bands
(see [The capacity model](../explanation/capacity.md)).

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
