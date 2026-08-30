# Architecture

## One process, two front doors

VolunteerDB is a single Python process. It serves both a server-rendered
web GUI (NiceGUI) and a JSON API (FastAPI). NiceGUI runs *on* FastAPI, so
both share one ASGI app, one event loop, and one async SQLAlchemy engine
against one PostgreSQL database. For a parish-scale system (~500
volunteers, tens of concurrent users) this buys a lot. There is no frontend
build, no API/frontend version skew, and one deployment unit, and
PostgreSQL's MVCC handles concurrency.

The two doors authenticate differently: the GUI with a signed session
cookie, the API with a personal Bearer token. But they converge immediately
on the same code below.

## The layering rule

```
src/volunteerdb/
  config.py      settings (env / .env)                        ┐
  env.py         Env: settings, clock, rng, mailer, http,     │
                 engine, sessions, the two mutable cells      │ infrastructure
  db.py          engine/session factories; transaction()     │ (the impure rim)
  models.py      SQLAlchemy models + history twins            │
  history.py     as-of query helpers                          ┘
  fp.py          Result = Ok | Err, and the combinators       ┐
  errors.py      DomainError: the closed set of refusals      │
  domain.py      NotifyMode, Outcome, the domain events       │ the core is pure
  policy.py      events → effects, decided from values        │ over a session
  permissions.py Actor: what may this user do?                │
  throttle.py    the rate-limit ledger, as a value            │
  services/      THE business layer — every read and write   │
  sheets/        roster CSV template/export/import            ┘
  effects.py     SendMail / Audit / ThrottleHit, and the ONE interpreter
  api/           FastAPI routers: Ctx, raise_http(), dispatch()
  ui/            NiceGUI pages: page_ctx(), run_command()
  jobs/          nightly one-shots: read → plan (pure) → execute
  scheduler.py   the in-app clock that runs jobs/: pure due rules, one object
```

The invariant that matters most: **`services/` is the only place business
rules live.** `api/` translates HTTP ↔ service calls; `ui/` translates
widget events ↔ service calls. Neither contains domain logic. That is why
the GUI and the API cannot drift apart on permissions or validation. Both
pass an `Actor` into the same functions (see
[The permission model](permissions.md)).

Three further rules hold the core still. Each one is a test that fails when
somebody breaks it:

### Errors are values

A service never raises. A fallible one returns `Result[T, DomainError]`
(`fp.py`): `Ok(value)` or `Err(error)`. The error is one of a closed set of
frozen values (`errors.py`). The set is `Forbidden`, `NotFound`, `Invalid`,
`Conflict`, `Throttled`, `External`, `WeakPassword`, `QueryError` and
`BadCredentials`.

`errors.message(err)` is the one user-facing text of each.
`api/deps.status_of()` is the one map to an HTTP status;
`ui/context.toast()` is the one map to a toast. A permission check is a
value too: `require(cond, what)` returns `Err(Forbidden)` or `None`, and
the idiom is

```python
if denied := require(actor.can_manage_team(team.id), "manage this team"):
    return denied
```

So a check that the code computes and drops is visible in the diff, and
`tests/test_authorization_layer.py` fails on a gate whose result nobody
uses. The edge that gets an `Err` rolls its transaction back. `await
session.rollback()` inside the `transaction()` block makes the block's exit
a no-op (`tests/test_uow.py`). So nothing partial ever commits without an
exception in sight. A constraint violation at commit is the one exception
left, and the boundary maps it to `Conflict` (409, a toast).

Infallible readers still return plain values. To wrap `teams.tree()` in
`Ok` would add `.value` at 30 call sites and no information.
`tests/test_purity_layer.py` sweeps the core for `raise`, the clock
(`datetime.now`, `date.today`), `settings()`, `secrets`/`uuid4` and
session creation. Its baseline is empty. Time, identifiers and
configuration arrive as parameters, which the edge supplies from its `Ctx`.
Those parameters are `now`, `today`, `tz`, `token`, `series_id` and
`site_terms`.

### Events, policy, effects

A service that writes returns `Outcome(value, events)`. The events are the
facts it established — `SubRequested`, `SlotHandedOver`, `EventCancelled`,
`InviteIssued`, `PasswordChanged`, `EmailChangeRequested`, … (`domain.py`).
An event carries what a mail template or an audit line needs, never a URL.

`policy.plan(events, PolicyCtx)` is a pure function from those events to
effects: `SendMail`, `Audit`, `ThrottleHit` (`effects.py`). It decides from
values: the moment, the link base, the door's notify mode, a snapshot of the
throttle ledger, the parish copy. The substitute-request cap, for instance,
is `policy` with a read of the ledger snapshot. The service writes the
request row either way, and the page tells the asker when nobody got mail.

One interpreter, `effects.run(effects, env)`, does them **after the
commit**. A page's `run_command()` runs it once the transaction closed; the
API's `dispatch()` hands it to `BackgroundTasks`. So mail never rides a
transaction, and a send that fails never turns work that committed into an
error. The two doors run the same rules over the same events. The JSON
API's `Ctx` carries `NotifyMode.digest`, and that one value is the whole of
"the API sends no roster mail". `tests/test_policy.py` pins every rule with
values alone; `tests/test_effects_layer.py` fails on a `send_email(`,
`audit_log(` or throttle charge under `api/`, `ui/` or `services/`.

### Env and composition roots

Everything impure the app touches is one frozen value, `env.Env`. It holds
the settings, a `Clock`, an `Rng`, a `Mailer`, `HttpClients`, and the
engine and session factory. It also holds the two mutable cells the process
legitimately holds (`ThrottleCell`, `QuotaCell`).

The app builds the `Env` only at composition roots, and no service ever
sees it. The roots are `main.run`, each job's `cli()`, `admin_bootstrap`,
and the seed, bench and `share_roster_sheets` scripts. The test suite's
`env` fixture and simulation main are roots too, with a `FakeClock`, a
`FakeRng`, a `RecordingMailer` and a `FakeHttp`. Services take values
(`now`, `tz`, `token`) and a session. The edges' `Ctx`/`PageCtx` carry the
`Env` to do effects with. There is no process-global engine and no
lazily-built session factory; the roots read `settings()`, and
`tests/test_purity_layer.py` says so.

Jobs follow the same split as pages. `read()` pulls plain rows in one
transaction. `plan()` is pure (`tests/test_jobs_plan.py` drives it with no
database). `execute()` sends through the Env's mailer and stamps each
person in a transaction of their own. The scheduler's due rules and state
transitions are pure functions over a frozen `JobState`. One `Scheduler`
object, built by `main.run`, owns the loop.

### What stays impure, and why

- `db.transaction()` and the two orchestrators that can open it,
  `services/roster_sheets` and `sheets/importer`. A sync interleaves network
  calls with several transactions and must not hold one open across them.
- The argon2 hasher and the audit/team-cache session listeners:
  infrastructure that the plan documents rather than threads through every
  call.
- `ThrottleCell` and `QuotaCell` on the `Env`: the rate limits and the mail
  allowance are genuinely process state. The code reads them as values (a
  ledger snapshot, a projection), and only the interpreter writes them.
- `VolunteerPanel` and `CytoscapeGraph` hold widgets, which is all they
  hold. A page loads inside one `page_ctx()` and renders after it; the
  widgets and the URL are the only state. `tests/test_ui_layer.py` checks
  that: no `ui.*` call inside a session block, no `nonlocal`, no nested
  handler that writes into its parent's container.

`actor=None` means a trusted internal caller. Those are the nightly jobs,
the seed and bench scripts, the roster sync, and a handful of services. Those
services act for a caller already authorized upstream. Each such call site
writes it out rather than defaults it, so a skipped check is visible in the
diff. The parameter is required, so a forgotten actor is a `TypeError`
rather than a silent bypass.

The GUI is server-side, so a page interaction (say, a role change on a
roster) follows one path. A websocket event calls `run_command(command)`.
That runs the service call in a transaction, commits, and runs the effects.
Then the page reloads from the database. There is no client-side data model
to reconcile.

Within a page file the shape is the same. The page function loads its data
and computes what the page needs. Then it reads as an outline: a header, a
list, and one call per section. Sections and action handlers are
module-level functions. They take ids and the rows they draw, not closures
over page state, so a reader can read each without the page around it.

The rule earns its keep on the detail pages. There the alternative was a
single 600-line function whose sections a reader could find only by a count
of indentation. NiceGUI's slot stack is dynamic, so a section called inside
`with frame(...)` builds into that frame exactly as inline code would.
`tests/test_app_surface.py` checks that every page still answers a browser
that types nothing but the URL.

## The domain model

Three tables carry the parish; the rest support them:

- **volunteer** — a person, with contact info and an admin-extensible JSONB
  `custom` bag ([custom fields](../how-to/custom-fields-and-workload.md)).
- **team** — a ministry; a `parent_team_id` that points at another team
  makes it a tree, and role rights cascade down it. An optional
  `workload_weight` feeds [workload](workload.md).
- **membership** — the pivotal entity: *volunteer X serves on team Y as
  role Z* (unique per volunteer+team). Nearly every question the app
  answers — rosters, vacancies, impact, the graph, workload — is a query
  over memberships.

Accounts (`app_user`) are deliberately separate from volunteers. Most
volunteers never log in, and a login's team rights derive from its *linked*
volunteer's memberships. History twins shadow all 3 core tables
([History and time travel](history.md)).

## Cross-cutting mechanisms

- **History** lives in the database (triggers), not the app; services
  cannot forget to write it.
- **As-of reads** are the same service functions with an optional
  timestamp; `history.py` swaps live tables for live∪history unions.
- **The app reads the team tree once per unit of work.**
  `services/teams.tree()` memoizes it on the session, keyed by the as-of
  timestamp. A session listener drops the memo the moment a write touches
  any team row (`team_cache.py`). The argument is the same as for the
  history triggers: invalidation is not something a service has to
  remember, because nothing has to remember it.
- **Email** (`services/mail.py`) degrades gracefully. Without an SMTP2GO
  key it logs messages instead of a send, which keeps development and the
  [test suite](../how-to/run-tests.md) offline.
- **The mail allowance is finite and the app counts it** (`mail_quota`,
  `services/mail_quota.py`). The provider's free tier is 200 messages a day
  and 1,000 a month. Past either, a send simply fails, which on a parish
  instance means a sign-in code that never arrives. Every message that
  actually leaves increments one row per parish day: a counter, never a log
  of who got what. Admins (only) get a header banner once the past week's
  trend says a day or the month will cross a cap. The same budget is why
  notifications batch the way they do: one digest a night per person, never
  one message per event.
- **Spreadsheets** (`sheets/`) are an alternative bulk interface to the
  same services, with the same all-or-nothing transactional behavior.

For where this process runs and how it reaches the database, see
[Production deployment architecture](deployment.md).
