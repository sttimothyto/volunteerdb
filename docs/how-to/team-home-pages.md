# Publish a team home page

- Each team can publish one page for its volunteers: rehearsal times, contact
  points, sign-up links. Nobody needs an account to read it.
- The content lives in a **Google Doc** that the team already edits.
  VolunteerDB fetches it nightly, sanitizes it, and serves it at a public
  URL:
  - `https://vdb.example.org/ministries/` — the index of every published
    page
  - `https://vdb.example.org/ministries/<team>.html` — one page per team
- No sign-in is required to read these pages. Put nothing private in the
  doc; the page is exactly as public as the doc itself.

## Set it up (leader, second-in-command, or core member)

1. In Google Docs, share the doc as **“Anyone with the link — Viewer”**.
2. On your team's page in VolunteerDB, under *Volunteer home page*, click
   *Set home page doc*.
3. Paste the doc link (`https://docs.google.com/document/d/…`).
4. Click *Save*.
5. Click *Fetch now* to publish immediately. Otherwise the nightly job
   (03:00) picks it up.
6. Preview the result through the *Public page* link.

- Edits to the doc show up after the next nightly fetch, or on demand with
  *Fetch now*.
- To unpublish the page, clear the link (*Change* → *Clear*).
- Once the page is published, *Download QR Code to Public page* saves a PNG
  QR code of its URL. It is print-ready for bulletins, posters, or handouts.
- The code encodes the page's current address. That address comes from the
  team's name and parent. A rename or a move of the team changes the URL, so
  reprint after a rename.

## What gets published

- The doc's exported HTML, passed through an allowlist sanitizer. Text,
  headings, lists, tables, images and links survive. Scripts, frames and
  event handlers never do.
- VolunteerDB serves the page in a shell styled like the rest of the site.
  The title is the team's full path, and the page carries a "last updated"
  stamp.
- VolunteerDB downloads the images in the doc at fetch time and serves them
  itself. Google's export links expire after a while, so a hotlink would
  break.
- Limits per page: 30 images, 5 MB each, 20 MB total. The app downscales
  stills larger than 1600 px.
- An image that fails to download keeps its Google link, and so can
  eventually break. The rest of the page is unaffected.
- If a fetch fails (the doc was deleted, made private, or is over 2 MB), the
  team page in the app shows the error. The **last successful version stays
  published**.
- The app detects a doc that is not link-public: Google redirects the
  anonymous fetch to a sign-in page. The app reports the problem and does
  not cache the page.

## Permissions

- To set or clear the doc link, and to fetch on demand, you need full-roster
  rights on the team. That is its **leaders, seconds-in-command, and core
  team members**, plus admins (`Actor.can_view_full_roster`).
- The API mirror is `PATCH /api/teams/{id}/home-doc` with body
  `{"url": "…"}` (or `null` to clear).

## Operations

- Nightly refresh: `jobs.fetch_pages`, run at `VDB_FETCH_PAGES_AT` (03:00
  parish time) by the in-app scheduler. You can also run it by hand as
  `python -m volunteerdb.jobs.fetch_pages`. Output lands in the app journal
  (`journalctl -u volunteerdb-app`, grep `fetch_pages`). Each team fetches
  independently; one bad doc cannot block the rest.
- The `team_page` table caches the fetched HTML. The doc link lives on
  `team.home_doc_url` (history-versioned, so `changed_by` records who set
  it).
- Locally: run `make dev`, set a doc link on a seeded team, then open
  `http://localhost:8080/ministries/` in a private window.
