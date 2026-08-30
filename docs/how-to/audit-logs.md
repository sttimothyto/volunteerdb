# Read the audit log

Every database write and every authentication event produces a structured
log line. At higher verbosity, every read and every HTTP request does too.

- The line says **who** did it, **when**, **from where**, and **what
  exactly** changed.
- Logs go to stderr. In production that means journald, so the whole trail
  is available through `journalctl` with no extra tools.

This is the *readable operational* trail.

- The *tamper-resistant* record is the database-level
  [history mechanism](../explanation/history.md): triggers that archive
  every update and delete, whatever the application does.
- The two complement each other. The log also covers reads, inserts, and
  non-versioned tables. The triggers survive any application-level mistake.

## Line anatomy

```text
2026-07-30T14:02:11 [audit] db.update  changes={'phone': "None → '555-0100'"} ip=203.0.113.7 pk=id=12 table=volunteer txn=a1b2c3 user=3:jane@example.org via=gui
```

`user`
: `id:email` of the account that acts, a bare `id` where only the session
  id is known, or `-` for anonymous flows (login pages, bootstrap scripts).

`via`
: `gui` (browser session), `api` (Bearer-token request), or `-` (scripts,
  background work).

`txn`
: A short tag that all writes of one transaction share, together with the
  `db.commit` or `db.rollback` marker that closes it. A `db.rollback` line
  means the database did **not** apply the writes logged under that tag.
  Dry-run imports produce exactly this pattern.

Events to know, all at `AUDIT` unless noted:

- Rows: `db.insert` / `db.update` / `db.delete` (with full values or
  old → new diffs), `db.commit` / `db.rollback`, and `db.read` (at `INFO`
  and below only).
- Sign-in and accounts: `auth.login`, `auth.otp_requested`,
  `auth.api_token_issued`, `auth.invite_minted`, `auth.invite_redeemed`,
  `auth.password_set`, `auth.password_cleared`,
  `auth.email_change_requested`, `auth.email_changed`,
  `auth.email_change_cancelled`, `volunteer.address_replaced_by_other`.
  Refusals log at `WARNING`, so they show at the default level too:
  `auth.login_failed`, `auth.throttled`, `auth.invite_invalid`,
  `auth.email_change_invalid`, `auth.password_change_denied`,
  `auth.api_token_invalid`.
- Events: `event.collaboration_added`, `event.slot_handed_over`,
  `event.self_removal`, `event.sub_request_capped`,
  `event.task_force_teardown`.
- Spreadsheets: `export.roster` (below), `import.finished`,
  `sync.team_finished`, `roster_sheet.created`, `roster_sheet.synced`.
- The scheduler: `scheduler.started`, `scheduler.job_started`,
  `scheduler.job_succeeded`; a failure is `scheduler.job_failed` at
  `ERROR`.
- `http.request`, one line per request, at `INFO`.

**Who read what.**

- The log records writes in full, but not reads.
- `db.read` sits at `INFO`, below the `AUDIT` default, so ordinary page
  views leave no row-level trail.
- Bulk reads do leave a trail, because those are the reads that carry the
  parish off the premises.
- Every roster export is one `export.roster` line at `AUDIT`, on both the
  GUI and the API, whatever the verbosity.
- That line carries `scope` (`parish`, or the list of team ids exported),
  `as_of` when the file is a snapshot, and `notes_included`. The export
  leaves out the notes column for a viewer who cannot read notes.
- `import.finished` was always at `AUDIT`.
- So "who downloaded the roster, and when" has an answer at the default
  level. "Who looked at one volunteer's page" does not, on purpose: that
  would be a line per page view.

Two events carry more:

- `auth.invite_minted` carries `revealed`. It says whether the caller saw
  the link, or the site only mailed it to the volunteer (see
  [Manage user accounts](manage-users.md)).
- `volunteer.address_replaced_by_other` records a leader or admin who
  redirects somebody else's address. The site also mails the address that
  the change replaces.

`VDB_LOG_LEVEL` controls the verbosity. See
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

- The log always renders credential values (`password_hash`, `otp_hash`,
  `api_token`, `invite_token`, `email_change_token`, `calendar_token`) as
  `«redacted»`.
- The log renders ballot scores (`score`) as `«redacted»` too, because
  ballots are secret.
- The log never records one-time codes or raw tokens at all.
- The log truncates long values (notes, custom fields) to keep the lines
  readable.

Known gaps, all still covered by the
[history triggers](../explanation/history.md):

- When you delete a volunteer or a team, PostgreSQL cascades the delete to
  its memberships (`ON DELETE CASCADE`). Those membership deletions produce
  no `db.delete` line.
- `db.read` lines name the tables queried, not the rows returned. A query
  answered from the session's identity map emits no line.
- journald retention is finite, and the journal is root-readable on the
  server. The log is an operational trail, not an archival record.
