---
name: verify
description: How to launch and drive this app for end-to-end verification (NiceGUI pages + JSON API against the podman Postgres).
---

# Verifying volunteerdb changes at the surface

Prereq: `podman compose up -d db` (live DB on 127.0.0.1:5432; `.env` provides
`VDB_*`). The app entry point is `uv run volunteerdb` (port 8080), but for
verification use a launcher with a cookie shim (below).

## The auth problem and its shim

NiceGUI pages render server-side on plain GET, so `curl` + cookie jar
exercises all page code — but the login form needs a websocket round-trip, so
you can't log in with curl directly. Solution: a scratchpad launcher that
wires the real app (`volunteerdb.main.create_app()`) plus one extra route:

```python
from volunteerdb.ui.context import establish_session


@ui.page("/login-dev/{user_id}")  # path MUST start with /login —
def dev_login(user_id: int):  # it rides the AuthMiddleware
    establish_session(user_id, remember=True)  # UNRESTRICTED_PREFIXES exemption
    ui.label(f"dev-login ok: user {user_id}")
```

(A raw `app.storage.user["user_id"] = ...` write is NOT enough — sessions
without a `session_expires_at` read as expired.)

then `ui.run(host="127.0.0.1", port=8123, storage_secret=settings().storage_secret,
reload=False, show=False)`. Run it with `uv run python <launcher>` in the
background; ready when `curl http://127.0.0.1:8123/login` returns 200.

## Driving it

- Cookie per role: `curl -c admin.jar http://127.0.0.1:8123/login-dev/<app_user id>`.
  Look up ids: `podman exec volunteerdb_db_1 psql -U volunteerdb -t -c
  "SELECT id, email, is_admin FROM app_user" volunteerdb`.
  Seed logins: admin@sttimothy.example (admin), maria.alvarez@example.org
  (leader), felix.garcia@example.org (member).
- Page evidence: the initial element tree is JSON embedded in the GET HTML —
  grep for row dicts (e.g. `"workload":"red"`), labels, or "Admins only.".
- JSON API: `POST /api/auth/login {"email","password"}` → Bearer token
  (seed passwords: changeme / volunteer / volunteer).
- History spot-checks go straight to Postgres via
  `podman exec volunteerdb_db_1 psql -U volunteerdb -t -c "..." volunteerdb`.
- As-of views: `?as_of=<ISO ts>` — remember URL-encode `+00:00` as `%2B00:00`.

## Gotchas

- `create_app()` returns None; it wires the global `nicegui.app`. `/api/*`
  routes sit in a deferred `_IncludedRouter` until startup — don't judge
  registration by iterating `app.routes` offline.
- The dashboard is `/`; anonymous GETs redirect to `/login`.
- Websocket-only flows (dialog saves, button handlers) can't be driven by
  curl — cover those via `/api` equivalents and say so in the report.
- Email: with `VDB_SMTP2GO_API_KEY` unset (dev default), outbound mail is
  printed to the launcher's stdout as `[MAIL]` lines — invite links and login
  OTP codes can be read from there.
