# HTTP API

All endpoints live under `/api` and speak JSON (`src/volunteerdb/api/`).
Request and response schemas are Pydantic models in
`src/volunteerdb/api/schemas.py`; a running instance serves interactive
OpenAPI documentation with the exact schemas at `/docs` — that is the
authoritative schema reference, which is why this page lists endpoints and
semantics only.

## Authentication

Every endpoint except `POST /api/auth/login` requires a Bearer token:

```
Authorization: Bearer <token>
```

Tokens are personal, issued by `POST /api/auth/login` (email + password),
and stored server-side only as SHA-256 digests. Each login issues a fresh
token and revokes the previous one. Accounts without a password (OTP-only)
cannot obtain API tokens — and removing a password on `/account` revokes the
token that was issued against it. Only active accounts authenticate.

Requests run in a single transaction with the acting user recorded, so
history rows written by API calls carry `changed_by`
(see [History and time travel](../explanation/history.md)).

## Common query parameters

`as_of` (ISO 8601 date or timestamp)
: Accepted by most GET endpoints on versioned entities: returns the state
  as of that moment. A naive timestamp is interpreted in server-local time.
  A **bare date means the end of that day**, so `as_of=2026-07-30` includes
  everything that happened on the 30th; write `as_of=2026-07-30T00:00:00` for
  its first instant. The GUI's date box resolves identically. A value that is
  not a valid ISO date or timestamp is a `422`.

`dry_run` (bool)
: `POST /api/import` only — validate and report without writing.

## Error contract

| Status | Meaning |
|---|---|
| 401 | Missing/invalid Bearer token (`WWW-Authenticate: Bearer`) |
| 403 | Authenticated but not permitted (`Forbidden`) |
| 404 | Entity not found (`LookupError`) |
| 409 | Constraint conflict, e.g. duplicate team name (`IntegrityError`) |
| 413 | Import workbook larger than 10 MB |
| 422 | Validation failure (`ValueError` / Pydantic) |
| 429 | Login throttled (5 failures per email or 30 per IP, per 15 min) |

## Endpoints

Minimum-permission column values refer to the
[permission matrix](permissions.md#permission-matrix); "signed in" means any
authenticated account.

### Auth — `api/auth.py`

| Method & path | Permission | Notes |
|---|---|---|
| `POST /api/auth/login` | — | `{email, password}` → `{token}`; throttled |
| `GET /api/auth/me` | signed in | Own account incl. `has_password` |

### Volunteers — `api/volunteers.py`

| Method & path | Permission | Notes |
|---|---|---|
| `GET /api/volunteers` | signed in | `q=` name/email search, `as_of=`; contact fields redacted per viewer. `include_inactive=true` is **admin only** (403 otherwise), matching the GUI |
| `POST /api/volunteers` | admin | 201 |
| `GET /api/volunteers/{id}` | signed in | `as_of=`; redacted per viewer |
| `PATCH /api/volunteers/{id}` | edit rights on the volunteer | `is_active` admin-only; `custom` merges validated custom-field values |
| `DELETE /api/volunteers/{id}` | admin | 204 |
| `PUT /api/volunteers/{id}/photo` | signed in | Upload/replace the headshot (multipart `file`). Any image ≤ 10 MB (413 above); stored EXIF-stripped, center-cropped, 400×400 JPEG ≤ 24 KB. Unreadable file → 422. **Deliberately open to every signed-in account** — no `can_edit_volunteer` gating |
| `GET /api/volunteers/{id}/photo` | signed in | The stored JPEG bytes; 404 when there is none |
| `DELETE /api/volunteers/{id}/photo` | signed in | 204; idempotent |
| `GET /api/volunteers/{id}/assignments` | signed in | Team/role list, `as_of=` |
| `GET /api/volunteers/{id}/timeline` | full profile view | All-time service spells |
| `GET /api/volunteers/{id}/impact` | full profile view | "If they leave" hole report, `as_of=` |
| `GET /api/volunteers/{id}/proposals` | admin, leader/second, or voting member | Proposals involving them, with `as_candidate`/`as_voter`/`appointed` flags; scoped like `GET /api/elections/proposals` |
| `POST /api/volunteers/{id}/invite` | admin, or full-roster rights on one of their teams | Create (or re-arm) their account and return it with `invite_token` + `invite_expires_at`. The one account-creating route that is not admin-only — see the [permission matrix](permissions.md#permission-matrix). Mints only: **it does not email**, so deliver the link yourself. 422 when the volunteer is archived, has no address on file, the address belongs to another account, or the account already carries a password or has been signed into; 409 if two callers race |

The volunteer list and detail responses carry `has_photo` (null on embedded
volunteer objects elsewhere). Browsers load images from the cookie-
authenticated `GET /photos/{id}?v=…` route instead — it sits behind the
normal session, not Bearer auth, so `<img>` tags and the graph canvas work.

### Teams — `api/teams.py`

| Method & path | Permission | Notes |
|---|---|---|
| `GET /api/teams` | signed in | Full directory with paths, `as_of=` |
| `POST /api/teams` | admin | 201 |
| `GET /api/teams/{id}` | signed in | `as_of=` |
| `PATCH /api/teams/{id}` | admin | `clear_parent`, `clear_workload_weight` flags |
| `PATCH /api/teams/{id}/home-doc` | full roster on the team (leader/second/core, admin) | `{"url": "https://docs.google.com/document/d/…"}`, or `null` to unpublish |
| `DELETE /api/teams/{id}` | admin | 204; parent of sub-teams is protected |
| `GET /api/teams/{id}/roster` | roster names on the team | Contact details/notes redacted per role, `as_of=` |

### Memberships — `api/memberships.py`

| Method & path | Permission | Notes |
|---|---|---|
| `POST /api/memberships` | manage the team | 201; upsert on (volunteer, team) |
| `PATCH /api/memberships/{id}` | manage the team | `role` only (rev 0011 dropped `joined_on`/`notes`) |
| `DELETE /api/memberships/{id}` | manage the team | 204 |

### Reports & graph — `api/reports.py`

| Method & path | Permission | Notes |
|---|---|---|
| `GET /api/reports/coverage` | admin or any leader/second | Teams missing leader/second; non-admins see only their managed teams; `as_of=` |
| `GET /api/graph` | signed in | Cytoscape.js elements; `team_id=` focus filter, `as_of=` |

### Users — `api/users.py` (all admin-only)

Account *management* is admin-only throughout. The one exception lives with the
volunteer it concerns: `POST /api/volunteers/{id}/invite` above lets a team's
leaders, seconds and core members create an account for one of their own people.

| Method & path | Notes |
|---|---|
| `GET /api/users` | List accounts |
| `POST /api/users` | 201; returns `invite_token` + `invite_expires_at`. Omitting `volunteer_id` links the volunteer at that address, if exactly one holds it. An optional `password` must clear the [policy](../explanation/auth.md#what-a-password-has-to-be) — 422 with the reason if not |
| `PATCH /api/users/{id}` | `is_admin`, `is_active`, `volunteer_id` (`null` unlinks; omit to leave alone) |
| `POST /api/users/{id}/reinvite` | Invalidates password, fresh invite token with a new expiry |
| `POST /api/users/provision` | Bulk-create accounts for volunteers with email, and link existing unlinked accounts → `{created, linked, skipped}` |

(api-import-export)=
### Import / export — `api/io.py`

| Method & path | Permission | Notes |
|---|---|---|
| `GET /api/export/parish.csv` | admin | Full roster export, `as_of=` |
| `GET /api/export/team/{team_id}.csv` | full roster on the team | Roster export (sub-teams included), `as_of=` |
| `GET /api/export/my-teams.csv` | leads/seconds any team | Union of managed teams, `as_of=` |
| `POST /api/import` | admin or leader/second (rows scoped) | Multipart `file=` (roster `.csv`); `dry_run=`; all-or-nothing; 10 MB cap |

Column layout: see the [spreadsheet format](spreadsheets.md).

### Custom fields — `api/custom_fields.py`

| Method & path | Permission | Notes |
|---|---|---|
| `GET /api/custom-fields` | signed in | `include_inactive=` |
| `POST /api/custom-fields` | admin | 201 |
| `PATCH /api/custom-fields/{id}` | admin | Label/options/position/visibility; `key` immutable |
| `DELETE /api/custom-fields/{id}` | admin | 204 |

### Workload — `api/workload.py`

| Method & path | Permission | Notes |
|---|---|---|
| `GET /api/workload/config` | admin or leader/second | Multipliers + bands |
| `PUT /api/workload/config` | admin | Validated (see [workload model](../explanation/workload.md)) |
| `GET /api/workload/scores` | signed in | Only volunteers whose workload the caller may see; `as_of=` |

### Elections — `api/elections.py`

Vacancies need no endpoint of their own: `GET /api/reports/coverage` already
returns `missing_leader`/`missing_second` with the same scoping.

Ballots are secret: no route ever returns an individual voter's scores —
only per-voter turnout flags and, once voting concludes, aggregates.

| Method & path | Permission | Notes |
|---|---|---|
| `GET /api/elections/proposals` | admin, leader/second, or voting member | Managed subtree ∪ own rolls; `team_id=`, `status=` filters |
| `POST /api/elections/proposals` | manage the team | 201; ≥1 candidate; duplicate open (team, role) → 409; bad deadlines → 422 |
| `GET /api/elections/proposals/{id}` | manage the team or on its roll | Candidates (with current commitments), roll with `has_account`/`has_voted`, tally once concluded |
| `PATCH /api/elections/proposals/{id}` | manage the team | Deadlines/notes; reopening nominations under cast ballots → 422 |
| `POST /api/elections/proposals/{id}/candidates` | manager or voting member | 201; nominating phase only; duplicate → 409 |
| `DELETE /api/elections/proposals/{id}/candidates/{cid}` | manage the team | 204; nominating phase only |
| `POST /api/elections/proposals/{id}/voters` | manage the team | 201; roll freezes when voting begins |
| `DELETE /api/elections/proposals/{id}/voters/{vid}` | manage the team | 204; nominating phase only |
| `PUT /api/elections/proposals/{id}/ballot` | on the roll | 204; whole ballot `{scores: {candidate_id: 0..5}}`, omitted = 0, revisable until the voting deadline |
| `GET /api/elections/proposals/{id}/tally` | manage the team or on its roll | STAR result; 422 while voting is still possible |
| `POST /api/elections/proposals/{id}/appoint` | manage the team | Flips status *and* creates/upgrades the membership; concluded phase only |
| `POST /api/elections/proposals/{id}/cancel` | manage the team | Any open phase; already decided → 422 |
| `POST /api/elections/proposals/{id}/new-round` | manage the team | 201; cancels the source, clones candidates + roll (never ballots) with fresh deadlines |

### Events — `api/events.py`

Every event belongs to one team; viewing follows the roster-names rule and
taking part (RSVP, sign-up, claiming) additionally requires actual
membership of that team, enforced in the service. Unlike the GUI, **these
mutations send no email** (the repo rule: mail goes out from UI handlers
after commit, and from the nightly digest) — an API-created assignment
still reaches its volunteer through `jobs.event_reminders`.

| Method & path | Permission | Notes |
|---|---|---|
| `GET /api/events` | signed in | Scoped to teams with roster-name rights (admins: all); `team_id=`, `from=`, `to=`, `include_cancelled=` filters |
| `POST /api/events` | manage the team | 201, returns a **list**: `repeat_weekly_until` materializes one event per week (inclusive, ≤ 1 year) sharing a `series_id`, slots copied; no slots ⇒ one unlimited `Volunteers` slot |
| `GET /api/events/{id}` | roster-name rights | Slots with entries and RSVPs; managers additionally get derived `attendance` once the event ended |
| `PATCH /api/events/{id}` | manage the team | Details/times; allowed on past events (corrected times recompute auto hours), cancelled → 422 |
| `POST /api/events/{id}/cancel` | manage the team | Resolves open sub requests with it; already cancelled → 422 |
| `POST /api/events/{id}/slots` | manage the team | 201; future events only |
| `PATCH /api/events/{id}/slots/{sid}` | manage the team | Shrinking capacity below occupancy → 422 |
| `DELETE /api/events/{id}/slots/{sid}` | manage the team | 204; blocked while occupied, and an event keeps ≥ 1 slot |
| `PUT /api/events/{id}/rsvp` | member of the team | 204; idempotent overwrite `{available, note?}` (the ballot idiom) |
| `POST /api/events/{id}/slots/{sid}/assignments` | self: member; with `volunteer_id`: manager | 201; capacity full → 422, one slot per person per event → 422/409. Self sign-ups may pass `repeat_series: true` to copy the sign-up onto later weeks of the series (same slot name; full/conflicting weeks skip) |
| `DELETE /api/events/assignments/{aid}` | owner or manager | 204; future events only — past rosters are the attendance record |
| `POST /api/events/assignments/{aid}/sub-request` | owner or manager | 201; one open request per assignment → 422 |
| `POST /api/events/sub-requests/{id}/claim` | member not already serving that event | First-come; the assignment moves to the caller, losers → 422 |
| `POST /api/events/sub-requests/{id}/cancel` | requester or manager | Already resolved → 422 |
| `PATCH /api/events/assignments/{aid}/attendance` | manage the team | `{attended?, hours?}`, nulls clear back to auto; past non-cancelled events only |
| `GET /api/volunteers/{id}/hours` | view the volunteer's profile | Derived totals over past, non-cancelled events |

## Worked examples

See [Use the JSON API](../how-to/api-recipes.md) for curl recipes.
