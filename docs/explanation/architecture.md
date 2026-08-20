# Architecture

## One process, two front doors

VolunteerDB is a single Python process serving both a server-rendered web
GUI (NiceGUI) and a JSON API (FastAPI). NiceGUI runs *on* FastAPI, so both
share one ASGI app, one event loop, and one async SQLAlchemy engine against
one PostgreSQL database. For a parish-scale system (~500 volunteers, tens of
concurrent users) this buys a lot: no frontend build, no API/frontend
version skew, one deployment unit, and PostgreSQL's MVCC handling
concurrency.

The two doors authenticate differently — the GUI with a signed session
cookie, the API with a personal Bearer token — but converge immediately on
the same code below.

## The layering rule

```
src/volunteerdb/
  config.py      settings (env / .env)          ┐
  db.py          engine, sessions               │ infrastructure
  models.py      SQLAlchemy models + history twins
  history.py     as-of query helpers            │
  permissions.py Actor: what may this user do?  │
  throttle.py    login rate limiting            │
  scheduler.py   in-app clock: runs jobs/ nightly ┘
  services/      THE business layer — all reads/writes go through here
  api/           FastAPI routers: thin JSON wrappers over services/
  ui/            NiceGUI pages: call services/ directly, render widgets
  sheets/        roster CSV template/export/import (used by api/, ui/, jobs/)
  jobs/          nightly one-shots: home-page fetch, digests, Drive roster sync
```

The invariant worth defending: **`services/` is the only place business
rules live.** `api/` translates HTTP ↔ service calls and maps exceptions to
status codes; `ui/` translates events ↔ service calls and renders. Neither
contains domain logic, which is why GUI and API cannot drift apart on
permissions or validation — both pass an `Actor` into the same functions
(see [The permission model](permissions.md)).

Authorization is the part of that worth spelling out, because it is the part
that used to be untrue. Every service function that mutates or reads scoped
data takes `actor` and refuses for itself, through `permissions.require()` or
one of the per-module gates around it (`_managed`, `_visible`,
`_require_self`, …). A front door may still ask an actor-shaped question that
has no single service behind it — "may this account use elections at all",
"which teams does the export scope to" — and `tests/test_authorization_layer.py`
keeps that list short by failing on any other `require()` under `api/` or
`ui/`. Its companion sweep fails on a service that accepts an `Actor` and never
checks it, which is the exact shape of the bug that let any ministry leader
read another team's contact details through a task force.

`actor=None` means a trusted internal caller: the nightly jobs, the seed and
bench scripts, the roster sync, and the handful of services that act for a
caller already authorized upstream. It is written out at every such call site
rather than defaulted, so skipping a check is visible in the diff, and the
parameter is required so a forgotten actor is a `TypeError` rather than a
silent bypass.

Because the GUI is server-side, a page interaction (say, changing a role on
a roster) is: websocket event → page handler → service call in a
transaction → widgets refresh from returned state. There is no client-side
data model to reconcile.

Within a page file the shape is: the page function loads its data, computes what
the page needs, and then reads as an outline — a header, a list, and one call per
section. Sections and action handlers are module-level functions taking ids and
the rows they draw, not closures over page state, so each can be read without
the page around it. The rule earns its keep on the detail pages, where the
alternative was a single six-hundred-line function whose sections could only be
found by counting indentation. NiceGUI's slot stack is dynamic, so a section
called inside `with frame(...)` builds into that frame exactly as inline code
would; `tests/test_app_surface.py` checks that every page still answers a
browser that types nothing but the URL.

## The domain model

Three tables carry the parish; the rest is supporting cast:

- **volunteer** — a person, with contact info and an admin-extensible JSONB
  `custom` bag ([custom fields](../how-to/custom-fields-and-workload.md)).
- **team** — a ministry; self-referencing `parent_team_id` makes it a tree,
  and role rights cascade down it. An optional `workload_weight` feeds
  [workload](workload.md).
- **membership** — the pivotal entity: *volunteer X serves on team Y as
  role Z* (unique per volunteer+team). Nearly every question the app
  answers — rosters, vacancies, impact, the graph, workload — is a query over
  memberships.

Accounts (`app_user`) are deliberately separate from volunteers: most
volunteers never log in, and a login's team rights derive from its *linked*
volunteer's memberships. All three core tables are shadowed by history
twins ([History and time travel](history.md)).

## Cross-cutting mechanisms

- **History** is implemented in the database (triggers), not the app —
  services cannot forget to write it.
- **As-of reads** are the same service functions with an optional
  timestamp; `history.py` swaps live tables for live∪history unions.
- **The team tree is read once per unit of work.** `services/teams.tree()`
  memoizes it on the session, keyed by the as-of timestamp, and a session
  listener drops the memo the moment any team row is written (`team_cache.py`).
  Same argument as the history triggers: invalidation is not something a
  service has to remember, because nothing has to remember it.
- **Email** (`services/mail.py`) degrades gracefully: without an SMTP2GO
  key it logs messages instead of sending, which keeps development and the
  [test suite](../how-to/run-tests.md) offline.
- **Spreadsheets** (`sheets/`) are an alternative bulk interface to the
  same services, with the same all-or-nothing transactional behavior.

For where this process runs and how it reaches the database, see
[Production deployment architecture](deployment.md).
