# Publish events to a Google Calendar

VolunteerDB keeps a public Google Calendar of its events, reconciling every
30 minutes: new events appear, edits follow, cancellations disappear. The
calendar is the parish Google account's own, but nobody creates it by hand
— the first sync that runs with the parish Google token makes it, names it
for the parish (`VDB_ORG_NAME` + " events"), shares it *anyone with the link
can see*, and remembers its id in the database. Every later run checks the
sharing is still exactly that before it writes a single event.

Events are only ever *created* inside VolunteerDB; the sync is one-way. What
gets published: title, time, location, description. Never slots, rosters,
or volunteer names. Write event descriptions knowing they are public.

The `/events` page shows the same events itself and offers calendar
subscriptions of its own — a parish-wide feed and a personal one — so
readers do not need the Google calendar at all; it exists for the people
who live in Google Calendar and for the "Add to Google Calendar" link.

## What it needs

The **parish Google token** — the same `VDB_SHEETS_CLIENT_ID`,
`VDB_SHEETS_CLIENT_SECRET` and `VDB_SHEETS_REFRESH_TOKEN` the
[roster spreadsheets](roster-spreadsheets.md) use — authorised with two
extra scopes. There is no calendar id to configure: the sync keeps the id
of the calendar it made in the `app_setting` table. While the token is
empty the sync logs "not configured", so the feature is safe to deploy
before provisioning.

## 1. Enable the Calendar API

In the parish Google Cloud project (the one whose OAuth client rclone and
the roster sheets already use — see [Backups](backup-restore.md) for where
those credentials live): *APIs & Services → Library → Google Calendar API →
Enable*. The Sheets API needed the same step once.

## 2. Authorise the parish token with the calendar scopes

On a machine with a browser (never the server), signed into the browser as
the parish account, with the **same client id and secret** the roster sheets
use (on the server they are `VDB_SHEETS_CLIENT_ID` / `_SECRET` in
`/etc/volunteerdb/env`, and readable in `/root/.config/rclone/rclone.conf`):

```bash
python scripts/google_authorize.py "$CLIENT_ID" "$CLIENT_SECRET"
```

The consent screen now asks for four scopes: the two the sheets already had
(`spreadsheets`, `drive.file`) and two for the calendar —

`calendar.app.created`
: make secondary calendars, and read and write events on them — *only
  calendars this client created*. The parish account's own calendars stay
  out of reach. It is the same shape as `drive.file`, and it carries the
  same rule: authorise with the same OAuth client every time, because a
  different client cannot see the calendar this one made.

`calendar.acls`
: read and set who may see a calendar. How the calendar is made public,
  and how every run checks that it still is.

Deliberately not the full `calendar` scope: nothing here reads, lists or
deletes anything the parish keeps for itself.

The script prints a new `VDB_SHEETS_REFRESH_TOKEN`. **Only that value
changes** — the client id and secret are the ones you passed in. The old
refresh token keeps working until it is replaced, so the sheet sync is never
without a token.

Smoke-test the new token before deploying — mint an access token and list
the account's calendars (an empty list is fine; a JSON body is what you are
looking for):

```bash
TOKEN=$(curl -s https://oauth2.googleapis.com/token \
  -d client_id="$CLIENT_ID" -d client_secret="$CLIENT_SECRET" \
  -d refresh_token="$REFRESH_TOKEN" -d grant_type=refresh_token \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://www.googleapis.com/calendar/v3/users/me/calendarList?minAccessRole=owner" | head
```

## 3. Deploy the token

Pass the new refresh token on one deploy; it lands in `/etc/volunteerdb/env`
and later deploys read it back, exactly like the SMTP2GO key
([Rotate secrets](rotate-secrets.md)):

```bash
VDB_SHEETS_REFRESH_TOKEN=... make deploy SITE=<your-site>
```

On an instance that has never had the sheet sync, pass
`VDB_SHEETS_CLIENT_ID` and `VDB_SHEETS_CLIENT_SECRET` on the same command.

## 4. Watch the first run

Within 30 minutes the scheduler runs the sync; run it by hand to watch the
calendar come into being:

```bash
podman run --rm --network volunteerdb --env-file /etc/volunteerdb/env \
  localhost/volunteerdb:latest python -m volunteerdb.jobs.calendar_sync
```

It logs `calendar_sync.calendar_created` with the new id and ends with a
line like

```
calendar sync: 12 pushed, 0 removed, 0 failure(s), 0 unmanaged, ACL ok
```

`ACL ok` is the assurance: the calendar's sharing is exactly *the parish
account owns it* plus *anyone may read it*. Open the calendar's public link
(the **/events** page shows it to admins; it is also
`https://calendar.google.com/calendar/embed?src=<calendar id>`) in a private
browser window to see what the public sees.

## Subscribing from your own calendar

The **Events** page shows the parish's events as a month grid in two views —
*My duties* (the events you hold a slot at, the default) and *Whole parish*
— and beside the view switch an **Add to your calendar** button opens a
panel for the view you are on:

- **Subscribe** (Apple Calendar, Outlook, Thunderbird): a `webcal://` link
  that opens the subscription dialog. The calendar then refreshes itself,
  about hourly, from the feed.
- **Google Calendar**: for the parish view, *Add to Google Calendar* opens
  Google with the parish calendar ready to add. For your own duties Google
  offers no one-click route, so the panel shows the feed address to paste
  into *Other calendars → + → From URL*.
- **Download a .ics file**: a one-time copy to import anywhere; it does not
  update.

The feeds are served by VolunteerDB itself, straight from the database —
they need no Google calendar at all and never lag it. The parish feed is
public, like the Google calendar. Your personal feed's address carries a
private token: anyone holding the address can read your duties, so if it
gets out, **Reset the address** in the same panel (also on *Your account*)
and subscribe again. The token is stored in clear so it can be shown again
every time you open the panel; it unlocks your duty list and nothing else.

## How the sync stays honest

**It checks the sharing before every write.** Each run lists the calendar's
access rules. The *anyone: reader* rule that makes the calendar public is
re-added if it is missing and repaired if somebody downgraded it — that rule
is what the calendar is for. Any rule that would let somebody *other than
the parish account* write or own the calendar is a different matter: it was
set on purpose by somebody holding the parish password, so the sync does not
remove it. It reports it (`calendar_sync.acl_problem` in the log, a count in
the summary line) and exits non-zero, which is what makes the scheduler send
the once-a-day alert mail (`VDB_ALERT_EMAIL`). The events still go up.

**It knows its own entries.** Every entry the sync creates carries a private
`vdb_managed` marker, and the reconcile only ever patches or deletes marked
entries. An entry somebody typed into the calendar by hand — the calendar
is meant to hold what VolunteerDB put there and nothing else — is counted
(`N unmanaged` in the summary line), named in the log
(`calendar_sync.unmanaged_entry`), and left where it is. Delete it in Google
Calendar, or create the event properly in VolunteerDB.

**A deleted calendar is replaced.** If somebody deletes the calendar in
Google, the next run notices (`calendar_sync.calendar_missing`), forgets
every event's calendar stamp, makes a new calendar and fills it on the same
run. The public link changes; the **/events** page shows the new one.

Change detection is a fingerprint of the pushed payload stored on the event
row, so an untouched event costs no API call. The usual failure is a revoked
refresh token (Google revokes on password resets and long disuse); the fix
is step 2 again — and since the token is shared, the roster sheet sync will
have stopped at the same moment.
