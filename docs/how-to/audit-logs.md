# Read the audit log

Every database write, authentication event, and (at higher verbosity) every
read and HTTP request produces a structured log line saying **who** did it,
**when**, **from where**, and **what exactly** changed. Logs go to stderr;
in production that means journald, so the whole trail is available through
`journalctl` with no extra tooling.

This is the *readable operational* trail. The *tamper-resistant* record
remains the database-level [history mechanism](../explanation/history.md) —
triggers that archive every update and delete regardless of what the
application does. The two complement each other: the log also covers reads,
inserts, and non-versioned tables, while the triggers survive any
application-level mistake.

## Line anatomy

```text
2026-07-30T14:02:11 [audit] db.update  changes={'phone': "None → '555-0100'"} ip=203.0.113.7 pk=id=12 table=volunteer txn=a1b2c3 user=3:jane@example.org via=gui
```

`user`
: `id:email` of the acting account, a bare `id` where only the session id is
  known, or `-` for anonymous flows (login pages, bootstrap scripts).

`via`
: `gui` (browser session), `api` (Bearer-token request), or `-` (scripts,
  background work).

`txn`
: Short tag shared by all writes of one transaction and by its closing
  `db.commit` or `db.rollback` marker. A `db.rollback` line means the writes
  logged under that tag were **not** applied — dry-run imports produce
  exactly this pattern.

Events to know: `db.insert` / `db.update` / `db.delete` (row writes, with
full values or old → new diffs), `db.read` (at `INFO` and below),
`db.commit` / `db.rollback`, `http.request`, `auth.login`,
`auth.login_failed`, `auth.otp_requested`, `auth.invite_redeemed`,
`auth.invite_minted`, `auth.api_token_issued`, `import.finished`,
`volunteer.address_replaced_by_other`, and the export events below.

**Who read what.** Writes are recorded in full, but reads are not: `db.read`
sits at `INFO`, below the `AUDIT` default, so ordinary browsing leaves no
row-level trail. Bulk reads do, because those are the ones that carry the
parish off the premises — `export.parish`, `export.team_roster` (with
`notes_included`, since the notes column is withheld from a viewer who may not
read notes) and `export.my_teams` are logged at `AUDIT` on both the GUI and the
API, whatever the verbosity. `import.finished` has always been. So "who
downloaded the roster, and when" is answerable at the default level; "who
looked at one volunteer's page" is not, deliberately — that would be a line
per page view.

`auth.invite_minted` carries `revealed`, which says whether the caller was
shown the link or it was only mailed to the volunteer (see
[Manage user accounts](manage-users.md)). `volunteer.address_replaced_by_other`
records a leader or admin redirecting somebody else's address, which also mails
the address being replaced.

Verbosity is controlled by `VDB_LOG_LEVEL` — see
[Configuration](../reference/configuration.md).

## Recipes

All audit lines since this morning:

```sh
journalctl -u volunteerdb-app --since today | grep '\[audit'
```

Follow writes live:

```sh
journalctl -u volunteerdb-app -f | grep '\[audit'
```

Everything one person did:

```sh
journalctl -u volunteerdb-app --since -7d | grep 'user=3:'
```

Every change to one record (here volunteer 12):

```sh
journalctl -u volunteerdb-app | grep 'table=volunteer' | grep 'pk=id=12'
```

Failed sign-in attempts:

```sh
journalctl -u volunteerdb-app | grep 'auth.login_failed'
```

## What is guaranteed — and what is not

Credential values (`password_hash`, `otp_hash`, `api_token`, `invite_token`)
are always rendered as `«redacted»`; one-time codes and raw tokens are never
logged at all. Long values (notes, custom fields) are truncated to keep
lines readable.

Known gaps, all still covered by the [history triggers](../explanation/history.md):

- Deleting a volunteer or team cascades to its memberships inside
  PostgreSQL (`ON DELETE CASCADE`); those membership deletions produce no
  `db.delete` line.
- `db.read` lines name the tables queried, not the rows returned, and a
  query answered from the session's identity map emits no line.
- journald retention is finite (and the journal is root-readable on the
  server); the log is an operational trail, not an archival record.
