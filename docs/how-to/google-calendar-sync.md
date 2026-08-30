# Publish events to a Google Calendar

VolunteerDB keeps a public Google Calendar of its events.

- The sync runs every 30 minutes: new events appear, edits follow, and
  cancelled events disappear.
- The calendar belongs to the parish Google account, but nobody creates it by
  hand.
- The first sync that runs with the parish Google token creates the calendar.
- The sync names the calendar for the parish (`VDB_ORG_NAME` + " events").
- The sync shares the calendar as *anyone with the link can see* and keeps
  the calendar id in the database.
- Every later run checks that the access rules are still exactly that before
  it writes one event.

The sync is one-way:

- Events are only ever *created* inside VolunteerDB.
- The sync publishes the title, time, location and description of each event.
- It never publishes slots, rosters or volunteer names.
- Write event descriptions as public text.

The *Events* page (`/events`) shows the same events itself:

- It offers calendar subscriptions of its own: a parish-wide feed and a
  personal feed.
- Readers do not need the Google calendar at all.
- The Google calendar exists for the people who live in Google Calendar, and
  for the *Add to Google Calendar* link.

## What it needs

- The **parish Google token**: the same `VDB_SHEETS_CLIENT_ID`,
  `VDB_SHEETS_CLIENT_SECRET` and `VDB_SHEETS_REFRESH_TOKEN` that the
  [roster spreadsheets](roster-spreadsheets.md) use.
- You must authorise that token with 2 extra scopes (step 2).
- There is no calendar id to configure. The sync keeps the id of the
  calendar it made in the `app_setting` table.
- While the token is empty, the sync logs "not configured". The feature is
  safe to deploy before you provision the token.

## 1. Enable the Calendar API

1. Open the parish Google Cloud project. It is the one whose OAuth client
   rclone and the roster sheets already use. [Backups](backup-restore.md)
   says where those credentials live.
2. Go to *APIs & Services → Library → Google Calendar API → Enable*.

The Sheets API needed the same step once.

## 2. Authorise the parish token with the calendar scopes

Do this on a machine with a browser, never on the server.

1. Sign in to the browser as the parish account.
2. Find the **same client id and secret** the roster sheets use. On the
   server they are `VDB_SHEETS_CLIENT_ID` / `_SECRET` in
   `/etc/volunteerdb/env`, and readable in `/root/.config/rclone/rclone.conf`.
3. Run the authorisation script:

   ```bash
   python scripts/google_authorize.py "$CLIENT_ID" "$CLIENT_SECRET"
   ```

4. Approve the consent screen. It asks for 4 scopes. The sheets already
   had 2 of them (`spreadsheets`, `drive.file`); the other 2 are for the
   calendar.

The 2 calendar scopes:

`calendar.app.created`
: Make secondary calendars, and read and write events on them, but *only on
  calendars this client created*. The parish account's own calendars stay
  out of reach. It has the same shape as `drive.file`, and it carries the
  same rule: authorise with the same OAuth client every time. A different
  client cannot see the calendar this one made.

`calendar.acls`
: Read and set who can see a calendar. This is how the sync makes the
  calendar public, and how every run checks that it still is.

The script does not ask for the full `calendar` scope, on purpose: nothing
here reads, lists or deletes anything the parish keeps for itself.

- The script prints a new `VDB_SHEETS_REFRESH_TOKEN`.
- **Only that value changes.** The client id and secret are the ones you
  passed in.
- The old refresh token stays valid until you replace it, so the sheet sync
  is never without a token.

Test the new token before you deploy it:

1. Mint an access token and list the account's calendars:

   ```bash
   TOKEN=$(curl -s https://oauth2.googleapis.com/token \
     -d client_id="$CLIENT_ID" -d client_secret="$CLIENT_SECRET" \
     -d refresh_token="$REFRESH_TOKEN" -d grant_type=refresh_token \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
   curl -s -H "Authorization: Bearer $TOKEN" \
     "https://www.googleapis.com/calendar/v3/users/me/calendarList?minAccessRole=owner" | head
   ```

2. Look for a JSON body in the output. An empty list is fine.

## 3. Deploy the token

1. Pass the new refresh token on one deploy:

   ```bash
   VDB_SHEETS_REFRESH_TOKEN=... make deploy SITE=<your-site>
   ```

   The token lands in `/etc/volunteerdb/env`, and later deploys read it
   back, exactly like the SMTP2GO key ([Rotate secrets](rotate-secrets.md)).

2. On an instance that has never had the sheet sync, pass
   `VDB_SHEETS_CLIENT_ID` and `VDB_SHEETS_CLIENT_SECRET` on the same command.

## 4. Watch the first run

The scheduler runs the sync within 30 minutes. To watch the calendar
appear, run the sync by hand:

```bash
podman run --rm --network volunteerdb --env-file /etc/volunteerdb/env \
  localhost/volunteerdb:latest python -m volunteerdb.jobs.calendar_sync
```

- The run logs `calendar_sync.calendar_created` with the new id.
- It ends with a line like this one:

```
calendar sync: 12 pushed, 0 removed, 0 failure(s), 0 unmanaged, ACL ok
```

- `ACL ok` is the assurance: the calendar's access rules are exactly *the
  parish account owns it* plus *anyone can read it*.

To see what the public sees, open the calendar's public link in a private
browser window.

- The *Events* page shows the link to admins (*Open in Google Calendar*).
- The link is also
  `https://calendar.google.com/calendar/embed?src=<calendar id>`.

## Subscribing from your own calendar

The *Events* page shows the parish's events as a month grid in 2 views:

- *My duties*: the events where you hold a slot. This is the default view.
- *Whole parish*: every team's events.

Beside the view switch, the *Add to your calendar* button opens a panel for
the view you are on:

- **Subscribe** (*Subscribe in Apple Calendar, Outlook or Thunderbird*): a
  `webcal://` link that opens the subscription dialog. The calendar then
  refreshes itself from the feed, about every hour.
- **Google Calendar**: for the parish view, *Add to Google Calendar* opens
  Google with the parish calendar ready to add. For your own duties, Google
  offers no one-click route. The panel shows the feed address instead; paste
  it into *Other calendars → + → From URL*.
- **Download a .ics file**: a one-time copy to import anywhere. It does not
  update.

VolunteerDB serves the feeds itself, straight from the database:

- The feeds need no Google calendar at all, and they never lag behind it.
- The parish feed is public, like the Google calendar.
- Your personal feed's address carries a private token. Anyone who holds
  the address can read your duties.
- If the address gets out, click *Reset the address* in the same panel
  (also on *Your account*). Then subscribe again.
- The site stores the token in clear, so the panel can show it again every
  time you open it. It unlocks your duty list and nothing else.

## How the sync stays honest

**It checks the access rules before every write.**

- Each run lists the calendar's access rules.
- The *anyone: reader* rule makes the calendar public. That rule is what the
  calendar is for.
- If the rule is absent, the run adds it again. If somebody downgraded it,
  the run repairs it.
- A rule that would let somebody *other than the parish account* write or
  own the calendar is a different matter. Somebody who holds the parish
  password set it on purpose, so the sync does not remove it.
- The sync reports such a rule (`calendar_sync.acl_problem` in the log, a
  count in the summary line) and exits non-zero.
- A non-zero exit makes the scheduler send the once-a-day alert mail
  (`VDB_ALERT_EMAIL`).
- The events still go up.

**It knows its own entries.**

- Every entry the sync creates carries a private `vdb_managed` marker.
- The reconcile only ever patches or deletes marked entries.
- The calendar is meant to hold what VolunteerDB put there and nothing else.
- The sync counts each entry that somebody typed into the calendar by hand
  (`N unmanaged` in the summary line). It names the entry in the log
  (`calendar_sync.unmanaged_entry`) and leaves it where it is.
- Delete such an entry in Google Calendar, or create the event properly in
  VolunteerDB.

**It replaces a deleted calendar.**

- If somebody deletes the calendar in Google, the next run notices
  (`calendar_sync.calendar_missing`).
- That run forgets every event's calendar stamp, makes a new calendar, and
  fills it.
- The public link changes. The *Events* page shows the new one.

**Change detection, and the usual failure.**

- Change detection is a fingerprint of the pushed payload, stored on the
  event row. An untouched event costs no API call.
- The usual failure is a revoked refresh token. Google revokes a token on a
  password reset and after long disuse.
- The fix is step 2 again.
- The token is shared, so the roster sheet sync stopped at the same moment.
