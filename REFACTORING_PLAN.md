# Refactoring plan: readability, separation of concerns, decoupling

**Status:** drafted 2026-09-05 from a full read of the tree at `54bcc57`;
executed step by step from that date. Each step below is ticked when its
commit landed with the full suite green. Decisions Ben made before execution
began are recorded under *Decisions* and are not up for re-litigation here.

The goal is maintainability: a reader should be able to open any page, route
or service and understand it without the rest of the tree in their head.
Public APIs and external behaviour stay as they are: every module keeps its
import path and every function its signature, either directly or through a
re-export; the JSON API's wire shapes and status codes do not change; the
GUI's labels and test markers do not change. Where a step could not keep a
detail identical, the step says so.

## Decisions

- **429 detail text may change.** The API's throttle refusals move onto the
  one phrasing `errors.message` defines for `Throttled`, and gain a
  `Retry-After` header. Status codes are unchanged.
- **The GUI dogfoods the API layer, and that stays.** `ui/` may import from
  `api/deps`; `api/` never imports from `ui/`. The shared edge plumbing lives
  in `api/deps`, and `ui/context` builds on it. No neutral third module.
- **No package splits, no importer decomposition.** File length is not the
  concern; readability is. `services/events.py`, `services/users.py`,
  `ui/events_page.py`, `ui/teams_page.py`, `api/schemas.py` and
  `sheets/importer.apply_rows` keep their shape.
- **Schema and migration changes are allowed** where they make the code
  clearer. None of the steps below needs one; a step that finds it does says
  so in its commit.
- **Shared test factories are allowed** where they make a test clearer.
- Standing decisions from the functional refactoring still hold: no reactive
  stores, no FP library, the JSON API runs `NotifyMode.digest`, the four
  ratchet sweeps stay at zero.

## What the read found

The layering from the functional refactoring is sound and its four ratchets
hold at zero. What remains is second-order, and it is what the steps address:

| Symptom | Where | Count |
|---|---|---|
| `Ctx` vs `PageCtx`, `perform`, `throttled`, `_split` vs `split_outcome` duplicated between doors | `api/deps.py`, `ui/context.py` | 4 pairs |
| `str(request.base_url).rstrip("/")` re-derived | api, ui pages, raw routes | 17 |
| Client IP re-derived, with two different fallbacks | api/auth, login, account | 6 |
| Throttle pre-check + attempt charge hand-rolled per door | api/auth ×3, login, account ×2, volunteers edit | 7 |
| `errors.Throttled` constructed | anywhere | 0 |
| Bare `raise HTTPException(4xx)` in routers | api/* except deps | 26 |
| Edges querying the ORM directly | `ui/teams_page.py`, `api/events.py`, logo and ministries routes | 8 |
| Post-hoc `out.x = …` mutation of API Out models | api/* | 27 |
| Page functions over 240 lines with nested dialogs closing over page state | six pages plus login | 7 |
| Hand-built dialogs / Cancel-Save rows | ui/* | 33 / 32 |

Three findings are more than duplication:

- A business rule lives at both doors and differs. Changing your own email
  address is refused by the API (`api/volunteers.py`, `update_volunteer`) but
  staged for confirmation by the GUI (`ui/volunteers_page.py`, the edit
  dialog's `save`).
- Two clocks in one page: `ui/events_page.py` reads `datetime.now(tz)` inside
  a page that already holds `ctx.now`, and `ui/calendar_routes.py` reads the
  system clock, bypassing the Env clock the tests control.
- `permissions.Forbidden` is a `PermissionError` that survives only to signal
  "not signed in" from `page_ctx`, and its name forces `ForbiddenValue`
  aliases at both doors.

## The steps

Every step keeps existing names working through re-exports or aliases, adds
or tightens one ratchet sweep, and ends with the full suite green in its own
commit.

- [x] **1. Baseline and ratchets.** Record the suite and coverage figure. Add
  `tests/test_edge_layer.py` with three baseline-driven sweeps that may only
  shrink: `current_env()`/`current()` calls per ui module; `session.get`,
  `session.execute` and `sa.select` under `ui/` and `api/`; request-origin
  and client-IP derivation outside `api/deps`.

- [ ] **2. `api/deps` becomes the one edge kernel.** `Ctx` gains `as_of` and
  takes `notify` as a constructor argument; `PageCtx` becomes a subclass, so
  there is one `policy_ctx()`. `perform` gains a `notify` keyword, `_split`
  becomes the public `split_outcome`, and `RequestFacts.from_request` replaces
  the origin and IP derivations. `ui/context` keeps its names and delegates.
  The two stray clock reads become the context's `now`.

- [ ] **3. One throttle gate.** `api/deps.throttle_gate(env, *keys, now,
  what)` returns `Err(Throttled)` or `None`, backed by a pure
  `throttle.retry_after`. The seven hand-rolled sites become gate, then
  charge, then call, and 429s flow through `to_http` and `toast`.

- [ ] **4. Untangle `ui/context.py` by concern.** The "not signed in" signal
  becomes `NotSignedIn` in `ui/context`; `permissions.Forbidden` goes, and
  with it the `ForbiddenValue` renames. The as-of banner, picker and parser
  move to `ui/asof.py` (re-exported from `context`).

- [ ] **5. Edges stop querying the ORM; the split rule is reunited.** Missing
  readers go into services (`branding.stamp`, `pages.published_image`,
  `events.slot_of_event`, `events.attendance_row`; the team page uses the
  gated `page_status` and `roster_sheet` readers that already exist). A pure
  `volunteers.address_change(actor, volunteer, typed)` answers unchanged,
  sync-login, plain, blank-own or needs-confirmation, and both doors consult
  it. The six private `_UNSET` sentinels become `fp.UNSET`.

- [ ] **6. API presenters in place.** Each hand-assembled Out model gets a
  constructor classmethod in `api/schemas.py` (`UserOut.of`, `UserOut.own`,
  `ProposalOut.of`, `VolunteerOut.redacted`, `EventDetailOut.of`, …),
  replacing the post-hoc mutations. The cross-router helpers move there, so
  routers stop importing each other. The contract test guards the wire shape.

- [ ] **7. Shared GUI vocabulary.** `ui/widgets.py` (role options and the
  phase, workload, role and inactive badges), `ui/forms.py` (dialog card,
  actions row, `confirm()`), `ui/tables.py` (one `wire_search`),
  `ui/guards.py` (the admin-only frame). The date formatter moves from `mail`
  to a core `timefmt` module, re-exported as `mail.event_when`, so the GUI and
  `task_force` stop importing mail templates for a formatter.

- [ ] **8. Read models, and pages as outlines.** `services/readmodels.py`
  holds `event_workroom`, `team_room`, `volunteer_profile` and
  `proposal_workroom`, each taking session, actor and now and returning a
  frozen value of plain data; the API detail endpoints reuse them where they
  overlap. The long page functions (events list and detail, team detail,
  volunteer detail, proposal detail, accounts, account settings, login) are
  rewritten to the shape `docs/explanation/architecture.md` describes: load
  the view, then one call per section, with dialogs and handlers at module
  level taking ids rather than closing over page state. The read models are
  listed in the authorization sweep's `SCOPING_ONLY`, each with its reason.

- [ ] **9. Jobs boilerplate.** `jobs.run_locked(name, main)` replaces the six
  identical `cli()` bodies; `jobs.send_digests` replaces the two digest
  loops. `main(env)` and `cli()` signatures stay.

- [ ] **10. Optional.** The two mailer transports and the two cells move out
  of `env.py` into their own modules, re-exported. The presentation fields on
  `Actor` are left where they are: that split was cut on 2026-08-18 and
  nothing makes it more urgent.

## Working rules

- One step, one commit, full suite green (`make test`), ruff clean.
- Stage explicit paths; never `git add -A`.
- Never run two database-backed pytest sessions at once; never edit files
  while a suite runs.
- A behaviour the step could not keep identical is named in the commit body.
