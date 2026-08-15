# Publish a team home page

Each team can publish one page for its volunteers — rehearsal times, contact
points, sign-up links — without anyone needing an account. The content lives
in a **Google Doc** the team already edits; VolunteerDB fetches it nightly,
sanitizes it, and serves it at a public URL:

- `https://vdb.sttimothyto.org/ministries/` — index of every published page
- `https://vdb.sttimothyto.org/ministries/<team>.html` — one page per team

No sign-in is required to read these pages, so put nothing private in the
doc — it is exactly as public as the doc itself.

## Set it up (leader, second-in-command, or core member)

1. In Google Docs, share the doc as **“Anyone with the link — Viewer”**.
2. On your team's page in VolunteerDB, under **Volunteer home page**, click
   **Set home page doc** and paste the doc link
   (`https://docs.google.com/document/d/…`).
3. Click **Fetch now** to publish immediately and preview the result via the
   **Public page** link. Otherwise the nightly job (03:00) picks it up.

Edits to the doc show up after the next nightly fetch, or on demand with
**Fetch now**. Clearing the link (Change → Clear) unpublishes the page.

Once the page is published, **Download QR Code to Public page** saves a PNG
QR code of its URL — print-ready for bulletins, posters, or handouts. The
code encodes the page's current address, which is derived from the team's
name and parent: renaming or moving the team changes the URL, so reprint
after a rename.

## What gets published

The doc's exported HTML, passed through an allowlist sanitizer: text,
headings, lists, tables, images and links survive; scripts, frames and
event handlers never do. The page is served in a shell styled like the rest
of VolunteerDB, with the team's full path as its title and a "last updated"
stamp.

Images in the doc are downloaded at fetch time and served from VolunteerDB
itself — Google's export links expire after a while, so hotlinking them
would break. Limits per page: 30 images, 5 MB each, 20 MB total; stills
larger than 1600 px are downscaled. An image that fails to download keeps
its Google link (and so may eventually stop rendering) without affecting
the rest of the page.

If a fetch fails — the doc was deleted, made private, or exceeds 2 MB — the
team page in the app shows the error and the **last successful version stays
published**. A doc that is not link-public is detected (Google redirects the
anonymous fetch to a sign-in page) and reported rather than cached.

## Permissions

Setting or clearing the doc link and fetching on demand require full-roster
rights on the team: its **leaders, seconds-in-command, and core team
members**, plus admins (`Actor.can_view_full_roster`). The API mirror is
`PATCH /api/teams/{id}/home-doc` with body `{"url": "…"}` (or `null` to
clear).

## Operations

- Nightly refresh: `python -m volunteerdb.jobs.fetch_pages`, run at 03:00
  from the host crontab (`volunteerdb-fetch-pages`, journal tag of the same
  name). Each team fetches independently — one bad doc cannot block the
  rest.
- The fetched HTML is cached in the `team_page` table; the doc link lives on
  `team.home_doc_url` (history-versioned, so `changed_by` records who set
  it).
- Locally: `make dev`, set a doc link on a seeded team, then open
  `http://localhost:8080/ministries/` in a private window.
