# Publish events to a Google Calendar

VolunteerDB can push its events onto a public Google Calendar owned by the
parish's Google account, reconciling every 30 minutes: new events appear,
edits follow, cancellations disappear. The `/events` page embeds the
calendar, and because the calendar itself is public, anyone with its link
can view it and save events into their own calendar — Google handles all of
that. Events are still only *created* inside VolunteerDB; the sync is
one-way, and entries someone adds to the calendar by hand are never
touched.

What gets published: title, time, location, description. Never slots,
rosters, or volunteer names. Write event descriptions knowing they are
public.

Four settings drive it (see
[Configuration](../reference/configuration.md)): `VDB_GCAL_CLIENT_ID`,
`VDB_GCAL_CLIENT_SECRET`, `VDB_GCAL_REFRESH_TOKEN`, `VDB_GCAL_CALENDAR_ID`.
While any of them is empty the sync logs "not configured" and the embed
stays hidden, so the feature is safe to deploy before provisioning.

## 1. Create the calendar

As the parish Google account (the same account behind the Drive sync), in
[Google Calendar](https://calendar.google.com):

1. *Other calendars → + → Create new calendar* — name it for the parish,
   e.g. "St. Timothy's Parish Events", and create it.
2. In the calendar's *Settings → Access permissions for events*, tick
   **Make available to public** with *See all event details*.
3. Under *Integrate calendar*, copy the **Calendar ID** (looks like
   `abc123...@group.calendar.google.com`) — that is
   `VDB_GCAL_CALENDAR_ID`. The same section shows the public URL to share.

## 2. Enable the Calendar API and reuse the OAuth client

In the parish Google Cloud project (the one whose OAuth client rclone and
the sheet decoration already use — see
[Backups](backup-restore.md) for where those credentials live):

1. *APIs & Services → Library → Google Calendar API → Enable* (the Sheets
   API needed the same step once).
2. Reuse the existing installed-app OAuth **client id and secret**. On the
   server they are readable in `/root/.config/rclone/rclone.conf`; they are
   `VDB_GCAL_CLIENT_ID` / `VDB_GCAL_CLIENT_SECRET`. The consent screen must
   be **In production**, which it already is for the Drive sync.

The Drive grant's `drive.file` scope does not cover Calendar, so a separate
authorization (next step) mints a separate refresh token; the rclone one is
left alone.

## 3. Authorize as the parish account

On a machine with a browser (never the server), signed into the browser as
the parish account:

```bash
python scripts/gcal_authorize.py "$CLIENT_ID" "$CLIENT_SECRET"
```

Approve the consent screen (scope: *calendar.events* only — the sync can
write events on calendars it is told about, nothing more). The script
prints `VDB_GCAL_REFRESH_TOKEN`.

Smoke-test the credentials before deploying — mint a token and read the
calendar:

```bash
TOKEN=$(curl -s https://oauth2.googleapis.com/token \
  -d client_id="$CLIENT_ID" -d client_secret="$CLIENT_SECRET" \
  -d refresh_token="$REFRESH_TOKEN" -d grant_type=refresh_token \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://www.googleapis.com/calendar/v3/calendars/$CALENDAR_ID" | head
```

A JSON body naming the calendar means everything lines up.

## 4. Deploy the settings

Pass the four values on one deploy; they land in `/etc/volunteerdb/env` and
later deploys read them back, exactly like the SMTP2GO key
([Rotate secrets](rotate-secrets.md)):

```bash
VDB_SITE=sttimothy \
VDB_GCAL_CLIENT_ID=... VDB_GCAL_CLIENT_SECRET=... \
VDB_GCAL_REFRESH_TOKEN=... VDB_GCAL_CALENDAR_ID=... \
  uvx pyinfra deploy/inventory.py deploy/deploy.py -y
```

Within 30 minutes the first reconcile pushes every upcoming event; run it
by hand to watch it happen:

```bash
podman run --rm --network volunteerdb --env-file /etc/volunteerdb/env \
  localhost/volunteerdb:latest python -m volunteerdb.jobs.calendar_sync
```

## How the sync stays honest

Every entry the sync creates carries a private `vdb_managed` marker, and
the reconcile only ever lists, patches, or deletes marked entries — a
retreat someone typed into the calendar by hand can never be clobbered.
Change detection is a fingerprint of the pushed payload stored on the event
row, so an untouched event costs no API call. Failures ride the scheduler's
alert email (`VDB_ALERT_EMAIL`), at most one per day; the usual cause is a
revoked refresh token (Google revokes on password resets and long disuse),
and the fix is re-running step 3.
