# HTTP API

- All endpoints live under `/api` and speak JSON (`src/volunteerdb/api/`).
- Request and response schemas are Pydantic models in
  `src/volunteerdb/api/schemas.py`.
- A live instance serves interactive OpenAPI documentation, with the exact
  schemas, at `/docs`.
- That documentation is the authoritative schema reference. This page lists
  endpoints and semantics only.

## Authentication

Every endpoint except `POST /api/auth/login` requires a Bearer token:

```
Authorization: Bearer <token>
```

- Tokens are personal. `POST /api/auth/login` (email + password) issues
  them. The server stores only their SHA-256 digests.
- Each login issues a fresh token and revokes the previous one.
- An account without a password (OTP-only) cannot get an API token.
- If you remove your password on `/account`, that revokes the token issued
  against it.
- Only active accounts authenticate.
- A request runs in one transaction, and the transaction records the user
  who acts. So the history rows written by API calls carry `changed_by` (see
  [History and time travel](../explanation/history.md)).

## Common query parameters

`as_of` (ISO 8601 date or timestamp)
: Most GET endpoints on versioned entities accept it. The endpoint returns
  the state as of that moment.
  - The server interprets a naive timestamp in server-local time.
  - A **bare date means the end of that day**. So `as_of=2026-07-30`
    includes everything that happened on the 30th.
  - Write `as_of=2026-07-30T00:00:00` for the first instant of that day.
  - The GUI's date box resolves identically.
  - A value that is not a valid ISO date or timestamp is a `422`.

`dry_run` (bool)
: `POST /api/import` only. Validate and report, but do not write.

## Error contract

| Status | Meaning |
|---|---|
| 401 | Missing/invalid Bearer token (`WWW-Authenticate: Bearer`), or bad credentials on login (`BadCredentials`) |
| 403 | Authenticated but not permitted (`Forbidden`) |
| 404 | Entity not found (`NotFound`) |
| 409 | Constraint conflict, e.g. duplicate team name (`Conflict`, the boundary's mapping of a dead transaction) |
| 413 | Import workbook larger than 10 MB |
| 422 | Validation failure (`Invalid`, `WeakPassword`, `QueryError`, or Pydantic) |
| 429 | Throttled (`Throttled`, with `Retry-After`): 5 failed sign-ins per email or 30 per IP per 15 min, 5 address changes per account per 15 min |
| 502 | An upstream service refused (`External`: Google Sheets, Google Calendar) |

- Every refusal is one of the `DomainError` values in `errors.py`.
- `detail` is `errors.message(err)`, the same sentence that the GUI shows as
  a toast.

## Endpoints

- The values in the Permission column refer to the
  [permission matrix](permissions.md#permission-matrix).
- "Signed in" means any authenticated account.

### Auth — `api/auth.py`

| Method & path | Permission | Notes |
|---|---|---|
| `POST /api/auth/login` | — | `{email, password}` → `{token}`; throttled |
| `GET /api/auth/me` | signed in | Own account incl. `has_password`, and `pending_email` + `email_change_expires_at` when an address change is waiting to be confirmed (never the token that would confirm it) |
| `PUT /api/auth/password` | signed in | 204; `{current_password, new_password}`. The current one is **always** required here, unlike the GUI: a token is only ever issued against a password, so a caller holding one can produce it. Failures charge the same throttle bucket as failed sign-ins (429); a weak new password is a 422 with the reason; the account's address gets a notice |
| `DELETE /api/auth/password` | signed in | 204; back to emailed-code sign-in. **Revokes the token making the call** — it was issued against the password being removed |
| `POST /api/auth/email-change` | signed in | 202 (nothing has moved yet); `{new_email}` → `pending_email` + `email_change_expires_at`. Mails the link to the address being claimed and a warning to the one being replaced. Throttled 5 per 15 min per account |
| `DELETE /api/auth/email-change` | signed in | 204; calls off a pending change, killing its link. Idempotent |
| `POST /api/auth/email-change/confirm` | — | `{token}` → the moved account. **Unauthenticated**, like the login: the token is the proof, and it is opened from whatever browser reads the mailbox. The account's address and the volunteer record behind it move together. Unknown, expired and already-spent links all answer 404 |
| `POST /api/auth/redeem-invite` | — | `{token, password?, agreed_to_confidentiality}` → the account. **Unauthenticated** for the same reason. Without a password the account stays email-code-only (and so cannot hold an API token). Refusing the confidentiality agreement is a 422; a spent or dead link is a 404 |

- **Deliberately absent: emailed-code sign-in.**
- The API issues a token against a password and revokes it with that
  password. So a mailbox round-trip must not produce one.
- An OTP-only account must set a password first if it wants API access.

### Volunteers — `api/volunteers.py`

| Method & path | Permission | Notes |
|---|---|---|
| `GET /api/volunteers` | signed in | `q=` substring search, `as_of=`; contact fields redacted per viewer. `q` matches **names** for everyone, and email/phone/notes/custom values only among volunteers the caller may see unredacted — a field rendered `•••` must not confirm itself by the row's presence. `include_inactive=true` is **admin only** (403 otherwise), matching the GUI |
| `POST /api/volunteers` | admin | 201 |
| `GET /api/volunteers/{id}` | signed in | `as_of=`; redacted per viewer |
| `PATCH /api/volunteers/{id}` | edit rights on the volunteer | `is_active` admin-only; `custom` merges validated custom-field values. Changing somebody **else's** `email` applies at once, as it must — a leader fixing a bounced address cannot wait on the person who cannot read their mail — and mails the address being replaced, so a redirect is never silent. Changing **your own** `email` is refused with 422: your address is also your login, so it moves only once the new one confirms itself, and this API sends no mail to run that exchange — ask on **/account** instead. Somebody else's address is an ordinary edit |
| `DELETE /api/volunteers/{id}` | admin | 204 |
| `PUT /api/volunteers/{id}/photo` | signed in | Upload/replace the headshot (multipart `file`). Any image ≤ 10 MB (413 above); stored EXIF-stripped, center-cropped, 400×400 JPEG ≤ 24 KB. Unreadable file → 422. **Deliberately open to every signed-in account** — no `can_edit_volunteer` gating |
| `GET /api/volunteers/{id}/photo` | signed in | The stored JPEG bytes; 404 when there is none |
| `DELETE /api/volunteers/{id}/photo` | signed in | 204; idempotent |
| `GET /api/volunteers/{id}/assignments` | signed in | Team/role list, `as_of=` |
| `GET /api/volunteers/{id}/timeline` | signed in | All-time service spells. Not gated on full profile view: who serves where is already parish-wide (the directory, the graph, the volunteer list), and service history is the same kind of fact — the GUI's profile page shows the timeline to every viewer too |
| `GET /api/volunteers/{id}/impact` | full profile view | "If they leave" hole report, `as_of=` |
| `GET /api/volunteers/{id}/proposals` | admin, leader/second, or voting member | Proposals involving them, with `as_candidate`/`as_voter`/`appointed` flags; scoped like `GET /api/elections/proposals` |
| `POST /api/volunteers/{id}/invite` | admin, or full-roster rights on one of their teams | Create (or re-arm) their account and return it with `invite_expires_at`. The one account-creating route that is not admin-only — see the [permission matrix](permissions.md#permission-matrix). **`invite_token` comes back only to an admin.** The link signs its holder in as that volunteer, and a leader may add anybody to their own team and then edit their address, so for a non-admin caller the link is instead **mailed** to the address on the volunteer's own record — the one place this API sends email, because the alternative is minting a credential that reaches nobody. 422 when the volunteer is archived, has no address on file, the address belongs to another account, or the account already carries a password or has been signed into; 409 if two callers race |

- The volunteer list and detail responses carry `has_photo`. It is null on
  embedded volunteer objects elsewhere.
- Browsers load images from the cookie-authenticated `GET /photos/{id}?v=…`
  route instead. That route sits behind the normal session, not Bearer auth,
  so `<img>` tags and the graph canvas work.

### Teams — `api/teams.py`

| Method & path | Permission | Notes |
|---|---|---|
| `GET /api/teams` | signed in | Full directory with paths, `as_of=` |
| `POST /api/teams` | admin | 201. An omitted `workload_weight` starts the team at 1; send `null` for 0 (excluded from workload scores) |
| `GET /api/teams/{id}` | signed in | `as_of=` |
| `PATCH /api/teams/{id}` | admin | `clear_parent`, `clear_workload_weight` flags |
| `PATCH /api/teams/{id}/home-doc` | full roster on the team (leader/second/core, admin) | `{"url": "https://docs.google.com/document/d/…"}`, or `null` to unpublish |
| `GET /api/teams/{id}/roster-sheet` | manage the team | The roster spreadsheet's link and the last sync's outcome; `null` before one is linked. Holding the link is edit access to the sheet, so this is management-gated |
| `PATCH /api/teams/{id}/roster-sheet` | manage the team | `{"url": "https://docs.google.com/spreadsheets/d/…"}`. Records the link only; 422 on anything that is not a Sheets URL, and on a sheet another team already uses |
| `POST /api/teams/{id}/roster-sheet/sync` | manage the team | `{"direction": "import"}` applies the sheet's rows then writes the result back; `"export"` overwrites the sheet from the database. Importing never removes anybody. 422 with the reason if the sheet is unshared or malformed |
| `DELETE /api/teams/{id}` | admin | 204; parent of sub-teams is protected |
| `GET /api/teams/{id}/roster` | roster names on the team | Contact details/notes redacted per role, `as_of=` |
| `GET /api/teams/{id}/page` | full roster on the team | Whether the public page is publishing, and when it last fetched; `null` when no doc is set. The HTML is not here — it is served to the world at `/ministries/<slug>.html` |
| `POST /api/teams/{id}/page/fetch` | full roster on the team | Refetch now instead of waiting for the nightly job. A failed fetch keeps the last good page and reports itself in `status` |

### Memberships — `api/memberships.py`

| Method & path | Permission | Notes |
|---|---|---|
| `POST /api/memberships` | manage the team | 201; upsert on (volunteer, team) |
| `PATCH /api/memberships/{id}` | manage the team | `role` only — a membership is otherwise identified by the pair it joins |
| `DELETE /api/memberships/{id}` | manage the team | 204 |

### Reports & graph — `api/reports.py`

| Method & path | Permission | Notes |
|---|---|---|
| `GET /api/reports/coverage` | admin or any leader/second | Teams missing leader/second; non-admins see only their managed teams; `as_of=` |
| `GET /api/reports/dashboard` | signed in | The dashboard's statistics. See below |
| `GET /api/graph` | signed in | Cytoscape.js elements; `team_id=` focus filter, `as_of=` |

- `GET /api/reports/dashboard` answers every caller, but answers each caller
  differently.
- It has no single permission because it is three tiers. The right that
  already governs the page a tier summarises gates that tier.

| Field | Who gets a value | Otherwise |
|---|---|---|
| `parish` | admin | `null` |
| `leadership` | admin, or anyone with full-roster rights (core/second/leader) | `null` |
| `leadership.teams_without_leader` / `.teams_without_second` / `.gap_teams` | admin or leader/second — the `GET /api/reports/coverage` gate | `null` / `[]` |
| `leadership.bands` | admin or leader/second, per volunteer (`can_view_workload`) | `null` |
| `personal` | any account linked to a volunteer | `null` |

- A `null` section was never computed: the queries behind it did not run. A
  `0` is a real count.
- `as_of=` answers the versioned figures from the snapshot. It nulls the
  live-only ones (events, elections, ballots, hours, account counts) and sets
  `live: false` to say so.
- Deliberately absent from `personal`: the caller's own workload band. Nobody
  sees their own. See [workload](../explanation/workload.md).

### Users — `api/users.py` (all admin-only)

- Account *management* is admin-only throughout.
- The one exception lives with the volunteer it concerns.
  `POST /api/volunteers/{id}/invite` above lets a team's leaders, seconds and
  core members create an account for one of their own people.

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
| `GET /api/export/team/{team_id}.csv` | full roster on the team | Roster export (sub-teams included), `as_of=`. The **Volunteer notes** column comes through blank unless the caller may read notes (admin or leader/second of the team) — everywhere else `notes` needs `can_edit_volunteer`, and the column stays in place so the file still round-trips |
| `GET /api/export/my-teams.csv` | leads/seconds any team | Union of managed teams, `as_of=`. The GUI's *Export team(s)* button on `/teams` covers the same ground, widened to core members |
| `POST /api/import` | admin or leader/second (rows scoped) | Multipart `file=` (roster `.csv`); `dry_run=`; all-or-nothing; 10 MB cap. Sends no mail: a redirect used to notify the mailbox it moved away from, but one pass over a messy sheet fires dozens at once against a 200-message day, mostly at the dead addresses being fixed. Redirects still land in `volunteer_history` |

- Column layout: see the [spreadsheet format](spreadsheets.md).

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

- Vacancies need no endpoint of their own. `GET /api/reports/coverage`
  already returns `missing_leader`/`missing_second` with the same scope.
- Ballots are secret. No route ever returns an individual voter's scores.
  Routes return only per-voter turnout flags and, after the voting deadline,
  aggregates.

| Method & path | Permission | Notes |
|---|---|---|
| `GET /api/elections/vacancies` | admin, leader/second, or voting member | Seats missing a leader or a second — where a proposal would go next. `GET /reports/coverage` is close but returns every team with its counts, not the holes |
| `GET /api/elections/proposals` | admin, leader/second, or voting member | Managed subtree ∪ own rolls; `team_id=`, `status=` filters |
| `POST /api/elections/proposals` | manage the team | 201; ≥1 candidate; duplicate open (team, role) → 409; bad deadlines → 422 |
| `GET /api/elections/proposals/{id}` | manage the team or on its roll | Candidates (with current commitments), roll with `has_account`/`has_voted`, tally once concluded |
| `PATCH /api/elections/proposals/{id}` | manage the team | Deadlines/notes; reopening nominations under cast ballots → 422 |
| `POST /api/elections/proposals/{id}/candidates` | manager or voting member | 201; nominating phase only; duplicate → 409 |
| `DELETE /api/elections/proposals/{id}/candidates/{cid}` | manage the team | 204; nominating phase only |
| `POST /api/elections/proposals/{id}/voters` | manage the team | 201; roll freezes when voting begins |
| `DELETE /api/elections/proposals/{id}/voters/{vid}` | manage the team | 204; nominating phase only |
| `GET /api/elections/proposals/{id}/ballot` | on the roll | The caller's **own** scores, so a ballot can be revised rather than overwritten blind. Empty until they vote. Nobody else's is readable anywhere, by anyone |
| `PUT /api/elections/proposals/{id}/ballot` | on the roll | 204; whole ballot `{scores: {candidate_id: 0..5}}`, omitted = 0, revisable until the voting deadline |
| `GET /api/elections/proposals/{id}/tally` | manage the team or on its roll | STAR result; 422 while voting is still possible |
| `POST /api/elections/proposals/{id}/appoint` | manage the team | Flips status *and* creates/upgrades the membership; concluded phase only |
| `POST /api/elections/proposals/{id}/cancel` | manage the team | Any open phase; already decided → 422 |
| `POST /api/elections/proposals/{id}/new-round` | manage the team | 201; cancels the source, clones candidates + roll (never ballots) with fresh deadlines |

### Events — `api/events.py`

- Every event belongs to one team.
- To view an event, a caller needs roster-name rights on its team (the
  roster-names rule).
- To take part (RSVP, sign-up, claim), the caller must also be a member of
  that team. The service enforces this.
- Unlike the GUI, **these mutations send no email**. The repo rule: mail goes
  out from UI handlers after commit, and from the nightly digest.
- An API-created assignment still reaches its volunteer through
  `jobs.event_reminders`.

| Method & path | Permission | Notes |
|---|---|---|
| `GET /api/events` | signed in | Scoped to teams with roster-name rights (admins: all); `team_id=`, `from=`, `to=`, `include_cancelled=` filters |
| `GET /api/events/mine` | signed in, linked to a volunteer | The caller's upcoming commitments, soonest first — what the dashboard counts, named |
| `GET /api/events/claimable` | signed in, linked to a volunteer | Open substitution calls the caller could take over |
| `GET /api/events/similar` | create events | The advisory double-booking check the GUI runs before creating: `starts_at`, `ends_at`, `location?`, `repeat_weekly_until?`. Never blocks; a hit on a team outside the caller's view comes back with `title` null |
| `GET /api/events/{id}/task-force` | roster-name rights | The task force behind the event, or `null` when one team staffs it alone |
| `POST /api/events/{id}/collaborators` | manage the team | 201; `{team_id}` invites another ministry to staff the event, creating the task-force team on the first call. Manage rights on the **event**, deliberately not on the team invited — and it confers no rights over that team's people |
| `POST /api/events/{id}/task-force/refresh` | manage the task force | Re-copies the source rosters, picking up anyone added since. Additive: strongest role wins, nobody is removed |
| `POST /api/events/assignments/{aid}/substitute` | owner or manager | `{volunteer_id}` hands the slot straight to a named teammate — the claim flow without the open call. The incoming volunteer must really be a member of the team |
| `POST /api/events` | manage the team | 201, returns a **list**: `repeat_weekly_until` materializes one event per week (inclusive, ≤ 1 year) sharing a `series_id`, slots copied; no slots ⇒ one unlimited `Volunteers` slot |
| `GET /api/events/{id}` | roster-name rights | Slots with entries and RSVPs; managers additionally get derived `attendance` once the event ended |
| `PATCH /api/events/{id}` | manage the team | Details/times; allowed on past events (corrected times recompute auto hours), cancelled → 422 |
| `POST /api/events/{id}/cancel` | manage the team | Resolves open sub requests with it; already cancelled → 422 |
| `POST /api/events/{id}/slots` | manage the team | 201; future events only. Optional `description` (≤ 300 chars) is a note beside the name, not part of it |
| `PATCH /api/events/{id}/slots/{sid}` | manage the team | Shrinking capacity below occupancy → 422. An explicit `"description": null` clears the note |
| `DELETE /api/events/{id}/slots/{sid}` | manage the team | 204; blocked while occupied, and an event keeps ≥ 1 slot |
| `PUT /api/events/{id}/rsvp` | member of the team | 204; idempotent overwrite `{available, note?}` (the ballot idiom) |
| `POST /api/events/{id}/slots/{sid}/assignments` | self: member; with `volunteer_id`: manager | 201; capacity full → 422, one slot per person per event → 422/409. Self sign-ups may pass `repeat_series: true` to copy the sign-up onto later weeks of the series (same slot name; full/conflicting weeks skip) and `notify_7d`/`notify_24h` (default true) to choose reminder stages |
| `DELETE /api/events/assignments/{aid}` | owner or manager | 204; future events only — past rosters are the attendance record |
| `POST /api/events/assignments/{aid}/sub-request` | owner or manager | 201; one open request per assignment → 422 |
| `POST /api/events/sub-requests/{id}/claim` | member not already serving that event | First-come; the assignment moves to the caller, losers → 422 |
| `POST /api/events/sub-requests/{id}/cancel` | requester or manager | Already resolved → 422 |
| `PATCH /api/events/assignments/{aid}/attendance` | manage the team | `{attended?, hours?}`, nulls clear back to auto; past non-cancelled events only |
| `GET /api/volunteers/{id}/hours` | view the volunteer's profile | Derived totals over past, non-cancelled events |

## Worked examples

- See [Use the JSON API](../how-to/api-recipes.md) for curl recipes.
