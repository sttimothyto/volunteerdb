# Functional refactoring of VolunteerDB

## Context

VolunteerDB (~26k lines: SQLAlchemy async + FastAPI JSON API + NiceGUI GUI in
one process, one Postgres) already has a defended layering rule — `services/`
is the only business layer, `api/` and `ui/` translate — and a good deal of
accidentally functional code: pure view-model builders
(`ui/teams_page.py:38 _hierarchy_rows`, `ui/calendar_grid.py`), pure mail
templates (`services/mail.py:81-462`), `scheduler.is_due`, `mail_quota.project`,
`teams.build_tree`, `query_lang`, `star`, `fieldcodec`, `passwords.problem()`.
What it lacks, verified against the tree:

- **Errors as values.** Services signal failure by raising: `ValueError` ×129
  carrying user-facing prose, `LookupError` ×48, `Forbidden` via
  `permissions.require()`, Google `RuntimeError` subclasses, and `_Abort` used
  as control flow in `sheets/importer.py:70`. Two edges each keep a copy of the
  exception→status/toast map (`api/deps.py:87-112`, `ui/context.py:120-133`).
  Some functions use `None` as the failure channel instead
  (`users.authenticate`, `verify_otp`); `users.start_otp_login` is tri-state.
- **Effects at the edge.** Orchestration lives in GUI handlers:
  `ui/events_page.py:188-285` interleaves the service call, `throttle`, mail
  composition, `mail.send_email` after commit, `audit_log`, `ui.notify`, reload.
  The API deliberately does none of that for the same mutation
  (`api/events.py:9-12`), and `events.substitute(..., caller_notifies=)` exists
  only to coordinate the two.
- **Injected time, config, randomness.** 18 inline `datetime.now(UTC)` in
  services (`users.py:54,383,401,422,483`, `elections.py:858,880,905`,
  `events.py:157 _now()` behind `_require_open`, the gate on every roster
  mutation), `settings()` at ~25 service sites, `secrets`/`uuid4` inside
  business logic, `sa.func.now()` as a query predicate at `events.py:862,913,1540`
  and `task_force.py:242` (DB clock and app clock already disagree under skew).
- **No process-global mutable state.** `db._engine/_sessionmaker` (lazy),
  `throttle._hits/_windows`, `mail_quota._cache` (read by
  `permissions.load_actor` on every admin request via a lazy import),
  `scheduler._task/_state`, `config.settings()` (`lru_cache`).
- **Own transactions inside "services".** `mail_quota.record_send/read_counts`,
  `roster_sheets.*` (7 sites; `sync_team` interleaves Google HTTP with three
  transactions), `sheets/importer.run_import/run_team_sync`,
  `history.fetch` (a second session from the global sessionmaker per as-of read).
- **A test tier without Postgres.** The autouse `TRUNCATE` fixture
  (`tests/conftest.py:172`) makes even the pure tests need a database.

Goal: the main interaction path — sign in → page → command → commit → mail —
becomes a pipeline of pure functions over values; every side effect (SQL,
HTTP, mail, clock, randomness, config, cookie, log) is performed at a named
edge by an interpreter; structural tests keep it that way.

## Decisions made with the user

| Question | Decision |
|---|---|
| FP toolkit | Hand-rolled `src/volunteerdb/fp.py`; no third-party library |
| Reactive UI | No. NiceGUI stays procedural and holds no state. "Reactive" lands as domain events → policy → effects, not UI stores |
| Result depth | Every service that can fail returns `Result`; **no exception crosses a service boundary** when done (see Interpretation below) |
| Scope | Everything: events, roster, volunteers, elections, auth/login/OTP/invites/account, jobs, scheduler |

**Interpretation of "every service returns Result"** (override at review if
you want the literal form): infallible readers (`teams.tree`, 34 callers;
`volunteers.search`; `get → T | None`; `name_map`) keep returning plain values —
wrapping them in `Ok` adds `.value` at every call site and no information. The
enforced invariant is the one that matters: zero `raise` under `services/`
(except `AssertionError`), verified by an AST sweep. If you prefer the
literal rule, those readers are annotated `Result[T, Never]` and return `Ok`.

**API mail (recorded, not changed):** once GUI and API share one policy, the
API keeps `NotifyMode.digest` (today's contract: "an API-created assignment
reaches its volunteer through the reminders job"). Flipping it later is one
line in `create_app` plus the API docstring and `docs/reference/http-api.md`.

## Target architecture

```
              ┌────────────── edges (impure, thin) ───────────────┐
  HTTP/API    │ api/deps.py   Ctx(session, actor, env, now)        │
  NiceGUI     │ ui/context.py PageCtx; run_command()               │
  jobs/*.cli  │ jobs/*.py     read → plan (pure) → execute         │
  scheduler   │ scheduler.py  tick(state, now) → (due, state)      │
              └──────┬───────────────▲──────────────▲──────────────┘
                     │ values        │ Result       │ effects
              ┌──────▼───────────────┴──────────────┴──────────────┐
  core        │ services/*  Result[Outcome[T], DomainError]         │
  (pure over  │ policy.py   DomainEvent → tuple[Effect, ...]        │
  a session)  │ permissions Actor predicates; errors.require()     │
              │ query_lang, fieldcodec, star, sheets/common, mail.* │
              └──────────────────────────────────────────────────────┘
              ┌────────────── adapters (impure, dumb) ─────────────┐
              │ db.transaction (UoW), Smtp2goMailer, HttpxClients,  │
              │ google_api transport, PIL, ThrottleCell, QuotaCell, │
              │ log/audit listeners, team_cache (per-UoW memo)      │
              └──────────────────────────────────────────────────────┘
```

Rules (each enforced by a sweep test from Phase 0):

1. Under `services/`, `sheets/`, `query_lang.py`, `passwords.py`,
   `permissions.py`, `policy.py`: no `settings()`, `datetime.now`, `date.today`,
   `time.monotonic`, `secrets.*`, `uuid4()`, `db_session()`, `sessionmaker()`,
   `httpx.AsyncClient`, `print`. Time, identifiers and configuration arrive as
   parameters. Two orchestrators (`roster_sheets`, `sheets/importer`) may open
   `db.transaction(env, …)` and nothing else may.
2. No `raise` under those paths except `AssertionError`. A fallible function
   returns `Result[..., DomainError]`. A gate's result is never discarded
   (a bare `require(...)` expression statement fails the sweep).
3. A service takes `session` first, `actor` second (unchanged); it never
   commits, never opens a transaction; the unit of work belongs to the edge.
4. A mutating service returns `Outcome(value, events)`; it never mails, never
   audit-logs, never touches a throttle. `policy.py` maps events to effects;
   the edge interpreter runs them **after commit**.
5. `ui/` holds no Python-side mutable state: no `nonlocal`, no dict/list
   mutated by a nested handler, no rendering inside a session block. Widgets
   and the URL are the only state; closures capture immutable values.
6. **A returned `Err` must abort the transaction.** Today rollback is free
   because exceptions unwind `session.begin()`; a returned value would commit
   partial writes. `await session.rollback()` inside the `begin()` block makes
   the block exit a no-op (verified: `TransactionalContext.__exit__` commits
   only while the transaction is active, SQLAlchemy 2.0.51). Pinned by
   `tests/test_uow.py` in Phase 0.

## The toolkit (new modules, dependency-free, tested without Postgres)

### `fp.py`

```python
@dataclass(frozen=True, slots=True)
class Ok[T]:  value: T
@dataclass(frozen=True, slots=True)
class Err[E]: error: E
type Result[T, E] = Ok[T] | Err[E]
# on both: is_ok(), map(f), map_err(f), bind(f), unwrap_or(default),
#          unwrap() -> raises errors.DomainErrorRaised(error) — transition shim
def ensure[E](condition: bool, error: E) -> Err[E] | None       # gate idiom
def some_or[T, E](value: T | None, error: E) -> Result[T, E]
def collect[T, E](results: Iterable[Result[T, E]]) -> Result[list[T], E]   # fail-fast
def attempt[T, E](fn, *exc, to_err) -> Result[T, E]
async def attempt_async[T, E](aw, *exc, to_err) -> Result[T, E]
async def lift[T](aw) -> Result[T, DomainError]    # legacy exceptions → errors.from_exception
def as_result(x: Result[T, E] | T) -> Result[T, E] # edges accept either during transition
```

Propagation idiom, documented once and used everywhere (no do-notation, no
`__bool__` on Result — never truth-test one):

```python
r = await team_service.get(session, team_id)
if isinstance(r, Err):
    return r
team = r.value
if denied := require(actor.can_manage_team(team.id), "manage this team"):
    return denied
```

`match` is reserved for branching on the *error* (`case Err(NotFound()):`).
`T | None` stays the idiom for "absent"; `some_or` turns absence into an
error where it is one.

### `errors.py`

Frozen dataclasses, not `Exception` subclasses; one transitional carrier.

```python
Forbidden(what)            NotFound(kind, key=None)     Invalid(message, field=None)
Conflict(message="conflicts with existing data")        Throttled(retry_after_s, what="")
External(service, message) WeakPassword(message)        QueryError(message)
BadCredentials(reason)     # reason for logs only; the client sees one phrase
type DomainError = Forbidden | NotFound | Invalid | Conflict | Throttled | External | WeakPassword | QueryError | BadCredentials

def message(err: DomainError) -> str            # the ONE user-facing phrasing (today's f-strings move here)
def require(condition, what="this action") -> Err[Forbidden] | None
def not_found(kind, key=None) -> Err[NotFound];  def invalid(message, field=None) -> Err[Invalid]
class DomainErrorRaised(Exception): error: DomainError      # deleted in Phase 7
def from_exception(exc) -> DomainError | None   # Forbidden→Forbidden, LookupError→NotFound, ValueError→Invalid,
                                                # IntegrityError→Conflict, WeakPassword/QueryError→same, Google errors→External
```

### `domain.py` and `effects.py`

```python
# domain.py — facts a service established; carries what a template needs, never a URL
class NotifyMode(StrEnum): direct = "direct"; digest = "digest"     # replaces caller_notifies
@dataclass(frozen=True) class Outcome[T]: value: T; events: tuple[DomainEvent, ...] = ()
SubRequested(team_id, event_id, title, path, slot, starts_at, ends_at, asker, note, audience: tuple[str, ...])
SlotHandedOver(event_id, assignment_id, title, slot, starts_at, ends_at, outgoing_name, incoming_email, notify)
SubClaimed(...)  SelfRemoved(...)  EventCancelled(title, path, starts_at, ends_at, emails)
InviteIssued(email, token, ttl_hours)  OtpIssued(email, code)  PasswordChanged(email, removed)
EmailChangeRequested(...)  AddressReplaced(was, now, volunteer_id)  RosterImported(report)

# effects.py — instructions for the world, run by ONE interpreter per edge
SendMail(to, subject, body)   Audit(event, fields: tuple[tuple[str, object], ...])   ThrottleHit(key)
type Effect = SendMail | Audit | ThrottleHit
async def run(effects: Sequence[Effect], env: Env) -> EffectReport   # never raises; SendMail → env.mailer, quota on success
```

(No `PushCalendar`: calendar sync is a 30-minute reconcile job, not a
per-mutation push — `jobs/calendar_sync.py`.)

### `policy.py` (pure; the "reactive" half — services emit, policy subscribes)

```python
@dataclass(frozen=True)
class PolicyCtx: now: datetime; base_url: str; notify: NotifyMode; throttle: throttle.Ledger; copy: mail.MailContext
def plan(events: Sequence[DomainEvent], ctx: PolicyCtx) -> tuple[Effect, ...]
# SubRequested → capped by the ledger snapshot? (Audit("sub_request.capped"),)
#                else (ThrottleHit(key), *(SendMail(a, *mail.sub_request_email(..., f"{ctx.base_url}/events")) for a in audience))
# SlotHandedOver → SendMail only if notify is direct and incoming_email; Audit always
```

### `env.py` (built only at composition roots; `app.state.env` is the holder)

```python
class Clock(Protocol):  def now(self) -> datetime                     # aware UTC
class Rng(Protocol):    def token(self) -> str; def otp_code(self) -> str; def uuid(self) -> UUID; def hex(self, n) -> str
class Mailer(Protocol): async def send(self, to, subject, body) -> bool
class HttpClients(Protocol): def client(self, *, timeout=10.0, follow_redirects=False) -> httpx.AsyncClient
SystemClock, SecretsRng, Smtp2goMailer(settings), LoggingMailer(settings), HttpxClients      # the real ones
class ThrottleCell: ledger; snapshot(); hit(key, now)                 # the ONE mutable throttle holder
class QuotaCell:    projection(sessions, today); record(sessions, day) # replaces mail_quota._cache
@dataclass(frozen=True)
class Env: settings; clock; rng; mailer; http; engine; sessions; throttle; quota; notify: NotifyMode
    tz -> ZoneInfo; mail_context() -> mail.MailContext; google() -> google_api.GoogleConfig
def build(settings=None, *, engine=None, clock=None, rng=None, mailer=None, http=None, notify=NotifyMode.direct) -> Env
def current() -> Env          # nicegui app.state.env; raises if create_app() never ran
```

Built in exactly: `main.run` → `create_app(env)`, `jobs/*.cli`,
`admin_bootstrap`, `scripts/seed.py`/`bench.py`, and the `env` test fixture
(`FakeClock`, `FakeRng`, `RecordingMailer`). Services never see `Env`; they
receive values (`now`, `tz`, `token`, `series_id`, `base_url` never — URLs are
edge data the policy adds).

### `db.py` (unit of work)

```python
def make_engine(url) -> AsyncEngine;  def make_sessions(engine) -> async_sessionmaker
@asynccontextmanager
async def transaction(env: Env, user_id: int | None) -> AsyncIterator[AsyncSession]
    # SET LOCAL app.user_id for the history triggers; commit on clean exit;
    # `await session.rollback()` inside the block aborts without an exception;
    # IntegrityError propagates (transaction is dead) and the boundary maps it to Conflict
# db_session()/init()/engine()/sessionmaker() stay as shims over env.current() until Phase 7
```

### Edge translators

```python
# api/deps.py
@dataclass(frozen=True) class Ctx: session; actor; env; now; base_url;  policy_ctx() -> PolicyCtx
def to_http(err) -> HTTPException   # Forbidden 403, NotFound 404, Invalid/WeakPassword/QueryError 422, Conflict 409,
                                    # Throttled 429, External 502, BadCredentials 401
def raise_http[T](result) -> T
def dispatch[T](ctx, background: BackgroundTasks, result: Result[Outcome[T] | T, DomainError]) -> T
    # raise_http; plan(events, ctx.policy_ctx()); background.add_task(effects.run, planned, ctx.env)
    # (the yield-dependency's commit runs before the response; BackgroundTasks after it → effects follow the commit)

# ui/context.py
@dataclass(frozen=True) class PageCtx: session; actor; env; now; base_url; as_of;  policy_ctx()
@asynccontextmanager async def page_ctx() -> AsyncIterator[PageCtx]      # replaces page_session/action_session
def toast(err: DomainError) -> None                                      # ui.notify(errors.message(err), color=...)
async def run_command[T](command: Callable[[PageCtx], Awaitable[Result[...]]], *, reload=True, on_ok=None) -> Result[T, DomainError]
    # Err → await session.rollback(); toast.   Ok → plan inside the tx; commit; effects.run AFTER commit; on_ok; ui.navigate.reload()
    # IntegrityError → Err(Conflict()) + toast

# jobs/__init__.py
def exit_code(err: DomainError) -> int          # External → 2, else 1, one structlog line
async def job_lock(env, name) -> AsyncIterator[bool]
```

## Phases

Each phase is green (`make lint && make test`) and committed on its own; each
shrinks a ratchet baseline in a sweep test so the next cannot regress the
last. Edges first, then services leaf-first, then orchestration, then pages,
then jobs, then the globals.

### Phase 0 — scaffolding, zero behaviour change (2 commits)

- New: `fp.py`, `errors.py`, `domain.py`, `effects.py`, `policy.py` (`plan`
  returns `()`), `env.py`, `tests/test_fp.py` (laws: map identity/composition,
  bind associativity, collect fail-fast order), `tests/test_errors.py`,
  `tests/test_uow.py` (rollback-inside-begin skips commit; IntegrityError
  propagates), `tests/test_purity_layer.py`.
- `api/deps.py` and `ui/context.py` each gain one `except DomainErrorRaised`
  clause delegating to the new translator — the interop shim for Phase 3.
- Pure tier: `pyproject.toml` `markers = ["pure: needs no database"]`;
  `tests/conftest.py::clean_tables` yields immediately for `pure`-marked tests
  and fetches `database`/`all_tables` via `request.getfixturevalue` otherwise.
  Mark the six already-pure modules (`test_fieldcodec`, `test_star`,
  `test_query_lang`, `test_ui_column_order`, `test_theme_contrast`,
  `test_config_surface`). `uv run pytest -m pure` runs without Postgres; CI
  runs it as a fast first step.
- Sweep (`tests/test_purity_layer.py`, modelled on
  `tests/test_authorization_layer.py`): AST over the rule-1 paths for the
  forbidden call names and for `raise`; `BASELINE: dict[str, Counter[str]]`
  records today's per-module counts; the test asserts each count `<=` baseline
  **and** that a baseline entry whose count reached zero has been deleted
  (self-cleaning, like `SCOPING_ONLY`). New test in
  `test_authorization_layer.py`: a GATE call as a bare expression statement
  fails (a discarded `Err` is a check that never refuses).

### Phase 1 — edges own `Env`, `Ctx`, the UoW, the throttle ledger, the quota cell (3 commits)

- `main.py`: `create_app(env=None)` sets `app.state.env`; `run()` builds it.
  `api/deps.py::api_ctx` yields the new `Ctx` (reads `env.sessions`,
  `env.clock.now()`). `ui/context.py::page_ctx`; `page_session`/`action_session`
  become aliases yielding `(ctx.session, ctx.actor)` so the 68 handler sites
  keep compiling.
- New `actors.py`: `load_actor(session, user, *, mail_quota=None)` moves out of
  `permissions.py` (which keeps `Actor`, `require`, `Forbidden`) and imports
  `services.teams` at top — the cycle at `permissions.py:191,214,226` is gone
  once `require` is service-free. Callers: `api/deps.py`, `ui/context.py`,
  `sheets/importer.py`, `services/roster_sheets.py`. The admin quota projection
  is passed in by `api_ctx`/`page_ctx` from `env.quota`.
- `history.py::fetch`: second session via `AsyncSession(bind=session.bind,
  expire_on_commit=False)`; the `db` import (and the reason `team_cache.py`
  had to be top-level) goes.
- `throttle.py`: frozen `Ledger`; pure `blocked(ledger, key, limit, window_s,
  now)`, `hit(ledger, key, now) -> Ledger`, `sweep(...)`; `ThrottleCell` in
  `env.py`. Callers (`api/auth.py`, `ui/login.py`, `ui/events_page.py`) use
  `env.throttle`. `tests/test_throttle.py` becomes `pure`; the autouse
  `reset_throttle` fixture clears `env.throttle`.
- `services/mail_quota.py`: `_cache` → `QuotaCell`; `projection(sessions,
  today)`, `record(sessions, day)`.
- `jobs/*.cli` build an `Env` and pass it to `main(env, …)`; `scheduler` passes
  its own. `tests/conftest.py` gains the `env` fixture (`FakeClock`, `FakeRng`,
  `RecordingMailer`); `db.db_session()` and `config.settings()` keep working as
  shims so untouched modules run unchanged.

### Phase 2 — gates return values (1 commit)

`errors.require(...) -> Err[Forbidden] | None` exists; `permissions.require`
(raising) stays until its last caller converts in Phase 3. Per-module gates
(`_managed`, `_visible`, `_require_self`, `_require_own_or_managed`,
`_require_member`, `_require_admin`, `_may_manage`, `get_managed`,
`_viewable`) will return `Result[T, DomainError]` or `Err | None` **under the
same names**, so `GATES` in `tests/test_authorization_layer.py:23-34` and its
`require(` text scan stay meaningful without edits.

### Phase 3 — services, leaf-first, one module per commit (~22 commits)

Per-module mechanical pattern, applied once so each module is touched once:
(a) every `raise X(msg)` → `return Err(...)`/`errors.invalid(msg)`;
(b) `require(...)` → `if denied := require(...): return denied`;
(c) `datetime.now`/`settings()`/`uuid4`/`new_token` → keyword-only value
parameters (`now: datetime`, `tz: ZoneInfo`, `token: str`, `series_id: UUID |
None`, `today: date`), supplied by the edge from `ctx.now`/`ctx.env`;
(d) unconverted callers call `.unwrap()`; converted callers of unconverted
callees use `await fp.lift(...)`;
(e) the module's tests replace `pytest.raises(X, match=...)` with `assert
err_of(r) == Invalid("...")` (helper `tests/fp_helpers.py`) — same message
strings, so the UI copy stays pinned;
(f) the module's `BASELINE` entry reaches zero and is deleted.

Reader archetype (`services/volunteers.py:103 search_or_query`): `search`
stays a plain reader; `search_or_query -> Result[list[Volunteer], QueryError]`
because `query_lang.compile_volunteers` now returns `Result[ColumnElement,
QueryError]`.

Mutator archetype (`services/events.py:1374 substitute`):

```python
async def substitute(session, actor, *, assignment_id, new_volunteer_id, acted_by,
                     notify: NotifyMode, now: datetime) -> Result[Outcome[EventAssignment], DomainError]:
    assignment = await session.get(EventAssignment, assignment_id)
    if assignment is None: return not_found("assignment", assignment_id)
    event = await _get(session, assignment.event_id)            # was _get_or_raise
    if isinstance(event, Err): return event
    event = event.value
    if denied := _require_own_or_managed(actor, assignment, event, "hand over this slot"): return denied
    if closed := _require_open(event, "substitute on this event", now): return closed   # was _now()
    vid = await _require_member(session, new_volunteer_id, event.team_id)
    if isinstance(vid, Err): return vid
    if assignment.volunteer_id == vid.value: return invalid("they already hold this slot")
    ...  # same writes, same flush()
    if notify is NotifyMode.direct: await _mark_notified(...)   # the in-transaction branch, named honestly
    else: <delete the stale Notification row>
    slot = await session.get(EventSlot, assignment.slot_id)
    return Ok(Outcome(assignment, (SlotHandedOver(event.id, assignment.id, event.title,
              slot.name if slot else "volunteer", event.starts_at, event.ends_at,
              outgoing.full_name, incoming.email, notify),)))
```

`is_past(event, now)` loses its default; the four `sa.func.now()` predicates
take the bound `now` (one clock instead of two).

Sequence (import edges verified): `teams` (`CycleError` → `Invalid`) →
`memberships` → `query_lang` (29 sites; `parse` stays, `compile_*` → Result)
→ `volunteers` → `custom_fields` → `photos`, `branding`, `workload`,
`reports` → `passwords` (`check -> Err[WeakPassword] | None`) + `auth.py`
(`new_token`/`new_otp_code` behind `Rng`; the argon2 hasher stays a module
constant, documented infrastructure) → `users` (`authenticate ->
Result[AppUser, BadCredentials]`; `start_otp_login -> Result[OtpStart,
DomainError]` with `OtpStart = CodeIssued(user, code) | CodeAlreadyLive(user)`;
`NoSuchAccount` is `Err(NotFound)`; tokens/codes arrive as parameters) →
`pages` (`fetch_and_store(session, actor, team_id, client, *, now)`; httpx
errors → `External("google_docs", …)`) → `events` (52 sites) → `task_force` →
`elections` (`today: date` required; `local_today` deleted) → `graph`, `ics`
(`render(..., tz, now)`), `stats` → `google_api` (`GoogleConfig` value +
injected client) → `gsheets`, `gcal` (→ `External`) → `mail` (templates take
`MailContext`; `send_email` → `Smtp2goMailer.send`; the module attribute stays
as a shim until the 18 `monkeypatch.setattr(mail, "send_email")` fixtures move
to `env.mailer.sent`) + `mail_quota` → `sheets/exporter`, `sheets/importer`
(`apply_rows -> Result[ImportReport, DomainError]`; `run_import(env, …)` uses
`transaction(env, user_id)` + `await session.rollback()` for dry-run; `_Abort`
deleted) → `roster_sheets` (`sync_team(env, team_id, *, direction, user_id) ->
Result[SyncOutcome, DomainError]`).

Size: 180 raise sites, 43 gates, ~25 clock/config/secret sites, ~243 edge
call sites touched once, 152 `pytest.raises` in 64 test modules.

### Phase 4 — events, policy, effects; `run_command` and `dispatch` (~10 commits, one per page/router)

- `policy.py` gets its real rules; `effects.run` its interpreter; `SmtpMailer`
  records the quota on success (the interpreter does, not `send_email`).
- UI handler archetype (`ui/events_page.py:188 _sub_request_dialog`):

```python
async def save() -> None:
    async def command(ctx: PageCtx):
        return await event_service.request_sub(
            ctx.session,
            ctx.actor,
            assignment_id=assignment_id,
            requested_by=ctx.actor.user.id,
            note=note.value,
            now=ctx.now,
        )

    def done(sub, effects):  # the toast is a pure function of the effects
        dialog.close()
        mailed = sum(isinstance(e, SendMail) for e in effects)
        ui.notify(
            CAPPED_TEXT if mailed == 0 else f"Asked {mailed} teammate(s)", color=...
        )

    await run_command(
        command, on_ok=done
    )  # rollback on Err → toast; effects after commit; reload
```

  `request_sub` gathers audience/paths/asker/slot itself and returns
  `Outcome(sub, (SubRequested(...),))`; the cap is decided by `policy.plan` from
  the `Ledger` snapshot in `PolicyCtx` (the row is still written — as today).
- API archetype (`api/events.py:531 substitute`): `assignment = dispatch(ctx,
  background, await event_service.substitute(..., notify=ctx.env.notify,
  now=ctx.now))`; the API app's `Env` is built with `NotifyMode.digest`, which
  reproduces "API sends no roster mail" exactly; the `Audit` effect replaces
  the inline `audit_log` so both doors log the same line.
- All 61 `@notify_errors` handlers → `run_command`; `api/auth.py` and
  `api/volunteers.py` `BackgroundTasks` mail → `dispatch`; `ui/login.py`,
  `account_page.py`, `invites.py` emit `OtpIssued`/`InviteIssued`/
  `PasswordChanged`/`EmailChangeRequested` events instead of composing mail.
- Sweep (`tests/test_effects_layer.py`): no `send_email(`, `audit_log(`,
  `throttle.` under `api/`, `ui/`, `services/`. `tests/test_policy.py` (pure):
  cap, notify mode, audience exclusion, per-event effect shapes.

### Phase 5 — stateless pages (~8 commits, one per page)

- Page shape: `vm = await load_<page>(ctx, params)` inside `page_ctx()`
  returning a frozen `<Page>VM` of plain values, session closed; then
  `render_<page>(vm, base_url)` with only `ui.*` calls. `_hierarchy_rows` is
  the template. The two pages that render inside the session today
  (`ui/events_page.py:1489-1546`, `ui/teams_page.py:797`) get `EventDetailVM`/
  `TeamPageVM`. (Safe: `models.py` has no `relationship()`, and
  `expire_on_commit=False` stays.)
- The mutable containers go: `teams_page.py:570` `state` dict → the upload
  handler renders the report and builds the Apply button with `content`
  captured; `photo_dialog.py:32`/`logo_dialog.py:36` same shape; `dashboard.py:62`
  graph filter → URL params (`/?q=&team=`; `_events_href` is the precedent);
  `search_box.py:65` race token → after the await, drop the result if the box
  text changed; `login.py:78` `pending_email` → read the widget; `invites.py`
  `resent` dict → re-render inside the dialog. `VolunteerPanel`/`CytoscapeGraph`
  stay (they hold widgets, nothing else — documented).
- Sweep (`tests/test_ui_layer.py`): no `ui.*`/`frame(` inside a
  `page_ctx()` block; no `nonlocal`; no free-variable `Subscript` store from a
  nested `def`. `notify_errors`, `action_session`, `page_session` deleted.
  `tests/ui_sim_main.py` unchanged (`establish_session` keeps its signature).

### Phase 6 — jobs and scheduler: read → plan → execute (~7 commits)

Job archetype (`jobs/event_reminders.py`):

```python
@dataclass(frozen=True) class ReminderRow: ...            # plain values, no ORM
@dataclass(frozen=True) class Digest: email; items: tuple[mail.EventDigestItem, ...]; stamps: tuple[tuple[int, NotificationStage], ...]
async def read(session) -> tuple[list[ReminderRow], dict[int, str], frozenset[tuple[int, NotificationStage]]]
def plan(rows, paths, already, *, today: date, tz: ZoneInfo) -> list[Digest]     # pure, @pytest.mark.pure
async def execute(digests, env) -> JobReport                                        # env.mailer; each stamp in transaction(env, None)
async def main(env: Env, today: date | None = None) -> int;  def cli(argv=None) -> int   # builds Env, logs JobReport (no print)
```

Same for `proposal_digest` (already read→decide→send), `roster_sync`
(`plan_sync`), `calendar_sync` (`diff(local_events, remote_items) ->
list[CalendarOp]` over `gcal.fingerprint`; executor runs ops, each stamping
in its own transaction), `fetch_pages`, `task_force_cleanup`.
`scheduler.py`: `SchedulerState = Mapping[str, JobState]` frozen;
`tick(state, now, jobs, settings) -> (due, state)` pure; a `Scheduler(env,
jobs)` object created in `main.run` owns the one `asyncio.Task` (replaces
`_task/_state`); `tests/test_scheduler.py` constructs one.

### Phase 7 — remaining globals, sweeps at zero, docs (2 commits)

- Delete `db._engine/_sessionmaker/db_session()/init()`; `scripts/seed.py`,
  `scripts/bench.py`, `admin_bootstrap.py` build an `Env`. `config.settings()`
  stays `lru_cache`d but the sweep confines it to `main.py`, `env.py`,
  `jobs/*::cli`, `admin_bootstrap.py`, `scripts/`, `tests/conftest.py`.
- Delete `DomainErrorRaised`, `fp.lift`, `errors.from_exception`,
  `install_exception_handlers`' legacy clauses, `permissions.require`
  (re-export `errors.require`). `team_cache`/`audit` listeners and the argon2
  hasher stay, documented as infrastructure.
- Docs: `docs/explanation/architecture.md` (new tree; "Errors are values";
  "Events, policy, effects"; "Env and composition roots"; "What stays impure
  and why"), `docs/how-to/run-tests.md` (`-m pure`), `docs/reference/http-api.md`
  (401/429/502), `docs/explanation/permissions.md` (gate returns), `README.md`
  architecture block. CI builds docs with `-W`, so stale cross-refs fail loudly.

## Interop during Phase 3

- Converted callee, unconverted caller: `(await teams.update(...)).unwrap()`
  raises `DomainErrorRaised`; the Phase-0 handler clauses map it, so
  403/404/422/409 and toasts are unchanged; that module's `pytest.raises` are
  rewritten in the same commit.
- Converted caller, unconverted callee: `r = await fp.lift(elections.appoint(...))`.
- `raise_http`, `dispatch`, `run_command` accept `Result | plain value`
  (`fp.as_result`) so a route/handler can adopt the helper before its services convert.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| A returned `Err` commits partial writes | Rule 6; `run_command` and the importer call `session.rollback()`; the API relies on `raise_http` unwinding `begin()`; `tests/test_uow.py` pins both; `test_events_service.py` gains "an Err leaves no row" |
| `flush()` is load-bearing (PKs, `after_flush` listeners in `audit.py`/`team_cache.py`, freeing partial unique indexes) | Every `flush()` stays; a `Result`-returning service still flushes before `Ok`. IntegrityError at flush is transaction-fatal → mapped to `Conflict` at the boundary, so `request_sub`'s unique-index race keeps its 409/toast |
| View models embedding live ORM objects after the session closes | `expire_on_commit=False` stays; no `relationship()` exists; VM dataclasses replace ORM objects only at the two in-session render sites |
| `history.fetch`'s second session | `AsyncSession(bind=session.bind)`; `test_history.py`/`test_team_cache.py` (`count_sql`) unchanged |
| `importer._Abort` | explicit `session.rollback()`; `tests/test_sheets*.py` assert on `ImportReport`, unchanged |
| `sa.func.now()` predicates | bound `now`; tests pass `now` via the `env` fixture's `FakeClock` (real clock by default) |
| `load_actor` lazy imports and quota call | `actors.py` imports `teams` normally; quota projection passed in from `env.quota` |
| `tests/test_authorization_layer.py` GATES sweep | gate names unchanged; sweep extended (discarded-gate test), not replaced |
| NiceGUI `user_simulation` wipes routes (`ui/__init__.py:21-27`); `tests/ui_sim_main.py` calls `establish_session` | `create_app()` re-sets `app.state.env` each call; the fixture's engine is shared; `establish_session` unchanged |
| API mail behaviour | `NotifyMode.digest` on the API `Env` preserves today's contract; the flip is a recorded, separate decision |
| Async + Result ergonomics | early-return idiom keeps indentation flat; no `bind_async`; the sweep forbids `raise`, so nobody can fall back |
| Churn (~180 raises, ~1,300 service call sites, 64 test modules) | one module per commit in dependency order; shim from Phase 0; ratchet baselines so nothing regresses silently |

## Verification

- Every phase: `make lint && make test` green (Postgres via `make db`,
  Chromium for `tests/e2e`). CI mirrors it; green `main` deploys.
- Pure tier: `uv run pytest -m pure` with the db container stopped must pass
  from Phase 0 on.
- Sweeps: `uv run pytest tests/test_purity_layer.py tests/test_authorization_layer.py tests/test_effects_layer.py tests/test_ui_layer.py -q`
  — baselines only shrink; a stale entry fails.
- Behaviour pins: `tests/test_api_contract.py` (status codes),
  `tests/test_ui_*` (`.mark()` locators, row dicts), `tests/e2e`,
  `tests/test_app_surface.py` (no anonymous route), `tests/test_uow.py`.
- End-to-end after Phase 4 (`/verify` skill: `make dev`, sign in as the seeded
  leader): request a substitute on an event; the `[MAIL]` lines and the audit
  line appear once each, after commit, from the interpreter; the same API call
  (`POST /api/events/assignments/{id}/sub-requests`) writes the row, logs the
  same audit line, and sends no mail (`digest`).
- Docs: `uv run --group docs sphinx-build -W -b html docs docs/_build/html`.

## Out of scope

Reactive UI stores or an FP library; schema or PL/pgSQL trigger changes;
changing `.mark()` locators or route paths; rewriting `api/schemas.py` (it
stays the edge's pydantic layer); flipping the API's mail mode.
