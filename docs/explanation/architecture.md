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

Because the GUI is server-side, a page interaction (say, changing a role on
a roster) is: websocket event → page handler → service call in a
transaction → widgets refresh from returned state. There is no client-side
data model to reconcile.

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
- **Email** (`services/mail.py`) degrades gracefully: without an SMTP2GO
  key it logs messages instead of sending, which keeps development and the
  [test suite](../how-to/run-tests.md) offline.
- **Spreadsheets** (`sheets/`) are an alternative bulk interface to the
  same services, with the same all-or-nothing transactional behavior.

For where this process runs and how it reaches the database, see
[Production deployment architecture](deployment.md).
