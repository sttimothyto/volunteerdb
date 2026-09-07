# UI/UX improvement plan: wayfinding, feedback, the long pages, the phone

**Status:** drafted 2026-09-06 at `b7bd22d`, from a read of `src/volunteerdb/ui/`
(9,644 lines across 41 modules), the user guide, the browser suite and 51 screenshots
of the seeded parish at 1280 px and 390 px, light and dark, as an administrator, a
leader, a member and an anonymous reader. Nothing here is built. It is executed step
by step, each step its own commit, the checkboxes below ticked as they land. The
decisions in the first section are proposals until Ben confirms them; after that
they are not re-litigated in the steps.

The goal is a site that a team leader can run from a phone in the parish hall and
that tells the reader, on every page, where they are, what just happened, and what
happens next — without changing what the site *is*. What must not change: the
visual identity (parchment, terracotta, Cinzel titles, the Greek-key rule); the GUI
rule of `docs/explanation/architecture.md:171-191` (a page loads, then renders; the
widgets and the URL are the only state; a command runs in a transaction and the
page re-reads the database — no client-side data model); `ui/` dogfooding `api/`;
the ratchets (`tests/test_theme_contrast.py`, `tests/test_ui_css_invariants.py`,
`tests/test_ui_layer.py`, `tests/test_docs_style.py` at a zero baseline, the `ty`
ceiling in `scripts/typecheck.py`); and `docs/guide/reference/screens.md`, which
describes every control and is updated in the same commit as the control.

## Decisions

- **The phone is a first-class reader, not a reflow target.** The reflow pass of
  2026-09-01 (`6fc5216`) made every page *fit* a 360 px screen; this plan makes the
  phone *usable*: the tables show what a phone can show, the controls are tap-first,
  and the tutorial's three-hop workaround for "open my own profile"
  (`docs/guide/tutorials/your-teams-and-your-service.md:45`) goes away because the
  workaround is no longer needed.
- **Keep the reload model; refresh a section only where the reload *is* the
  workflow's cost.** Forty of the 63 `run_command` call sites take the default
  `reload=True` (`ui/context.py:198,232`). For a one-off action that is fine. For the
  four loops a leader runs twenty times in a sitting — the roster, a slot's
  assignments, attendance, a ballot — the reload throws away scroll position, sort,
  filter text and the success toast on every iteration. Those four sections become
  `ui.refreshable` regions that re-run their own load-inside-`page_ctx()`,
  render-after-it builder. The GUI rule holds: nothing renders inside a session,
  nothing is cached in Python, the section re-reads the database. Everything else
  keeps the reload, made cheaper by carrying the reader's state in the URL and by a
  success message that survives it.
- **Anything that removes a record or takes a person off something asks first.**
  Eight actions do not (`What the read found`). The rule is mechanical so a sweep can
  hold it: every GUI handler that calls a removing service function awaits
  `forms.confirm`. Wording: the affirmative names the verb and the object ("Delete
  the team", "Remove Maria Alvarez from Hospitality", "Cancel the event"); the
  negative is "Cancel", or "Keep it" where the action is itself a cancellation.
- **Dates and times read as words, in one voice, from `timefmt.py`.** Seven output
  formats are live today; inputs stay `YYYY-MM-DD` / `HH:MM` with pickers (the
  ratchet at `tests/test_ui_css_invariants.py:122` already holds that). Display uses
  `event_when` for a detail line and one new short form for tables and badges.
- **A list longer than a screen is a table; a list of a handful is rows.** The roster,
  the accounts page and the team-weights editor are hand-rolled row lists with no
  search, sort or paging; at parish scale (478 volunteers, a 59-member team) they are
  the site's longest pages. They become `ui.table`, which also bounds the number of
  live components per page.
- **Improve the graph view.** At parish scale it is a hairball (screenshot: 478
  nodes, unreadable at 1280 px, 70 vh on a phone, mouse-only instructions).
  Remedy this to the best of your ability without losing any current function.
  It is okay for it to be hidden upon page view.
- **Every screen change lands with its `screens.md` line, its how-to, and its
  NiceGUI `User` test in the same commit.** The guide is at a zero STE baseline
  (`tests/test_docs_style.py:248`), so a changed control that is not written up
  fails the build, which is the point.
- **Nothing here changes the visual identity.** Colours move into tokens where they
  are still Tailwind literals; no new palette, no new type family without a
  separate decision (step 34 is optional and named as such).

Standing decisions from the earlier plans still hold: `ui/` imports `api/deps`
(a feature, not a smell); services take an `Actor` and `permissions.SYSTEM` is the
app acting for itself; the type ceiling only ratchets down; commits are per step,
staged by explicit path.

## What the read found

### By the numbers

| Symptom | Where | Count |
|---|---|---|
| `run_command` sites that hard-reload the whole page after success | `ui/context.py:232`; 40 of 63 call sites take the default | 40 |
| Removing actions with no confirmation | `teams_page.py:827` (delete a team, with every membership), `:962` (remove from roster), `volunteers_page.py:366` (remove from team), `events_page.py:1470` (leader removes someone from a shift), `:1513` (delete a slot), `admin_page.py:279` (new invite link — resets the password), `photo_dialog.py:110`, `logo_dialog.py:111` | 8 |
| Dialogs with input that a backdrop click or Escape discards | every `dialog_card`/`ui.dialog` (no `persistent` anywhere in `ui/`) | 20+ |
| Live date/time output formats | `timefmt.py:12` (words, 12-hour); `events_page.py:640` (`%Y-%m-%d %H:%M`); `:76` (`%a %b %-d`); `calendar_grid.py:76` (`%H:%M`); `dashboard.py:322` (`next %-d %b, %H:%M`); `account_page.py:251` (`%a %d %b, %H:%M`); `ministries_routes.py:146` (`%B %-d, %Y`); bare ISO dates on election badges (`widgets.py:51-53`) | 8 |
| Section titles that are `div`s (`text-lg font-medium`), not headings; real headings | 35 labels across 8 files; `a11y.heading()` called once (`layout.py:95`); the login and invite pages have no `h1` | 35 : 1 |
| `ui.notify` calls that pass `color=` only, never `type=` (so no icon: state by hue alone) | all of `ui/` | 76 |
| Loading, pending or disabled-while-awaiting states | none (`grep spinner|skeleton|loading|bind_enabled`: one hit, `photo_dialog.py:113`) | 0 |
| Inline field validation (`validation=`, `rules=`) | none; ~20 post-submit toasts instead (`volunteers_page.py:233`, `elections_page.py:66`, `events_page.py:277`, …) | 0 |
| Search mechanisms | suggestion menu (`search_box.py`), client-side filter (`tables.py:29`), `?q=` navigation; none on the roster, accounts, elections, fields, weights | 3 |
| Dialog widths in use | `w-96`, `w-[28rem]`, `w-[30rem]`, `w-[32rem]`, `w-[34rem]` | 5 |
| Dialogs that hand-roll their button row instead of `forms.actions` | `teams_page.py:769`, `events_page.py:397,998,1123`, `elections_page.py:151`, `photo_dialog.py:101`, `logo_dialog.py:104` | 7 |
| Hand-rolled row lists with no search, sort or paging | roster, accounts, candidates, voters, attendance, availability answers, team weights | 7 |
| Hard-coded Tailwind tints in pages | `text-gray-500` ×85, `-600` ×23, `-700` ×11, `-400` ×6, plus amber/red/blue; `theme.css:472-508` remaps some in each mode, not the same set | 152 |
| Tints used and remapped in neither mode | `text-amber-800` (`layout.py:140`, `account_page.py:255`), `text-red-800/900`, `bg-red-100` (`layout.py:130-132`) | 4 |
| Accessibility gaps deferred on 2026-08-24 | `docs/explanation/accessibility.md:116-126` | 4 |

### Seen in the screenshots

- **The team page for a 59-member team is 5,289 px tall at 1280 px and 11,848 px at
  390 px.** One row per member, each with its own `QSelect` for the role, the account
  badge, the hover-to-invite control and a remove icon; then the spreadsheet panel,
  the CSV importer and the home-page panel below it. No search, no paging.
- **Accounts is 2,665 px at 1280 px, 4,196 px at 390 px, for 33 accounts** — one row
  each with four icon-only buttons (link, key, ban, mail) whose meaning is in a
  tooltip. On the phone the icons wrap under the address. `screens.md:344` documents
  the icons because nothing on the page does.
- **At 390 px the events table shows 2 of its 6 columns** (When, Event) and the
  volunteers table 2 of 9 (Name, Email); the rest scroll sideways inside the table.
  ISO timestamps (`2026-09-10 19:30`) take a third of the visible width.
- **The dashboard leads an administrator with a 478-node graph** (about 630 px of the
  1,789 px page) whose caption says "hover a node to isolate its connections"; on a
  phone the same graph is 70 vh. The bands above it (Parish, Needs attention) and
  below it (My teams, My service) are the useful part. The Guides band is 40 links in
  running lines.
- **The header never says where you are.** Seven identical nav buttons on every page
  (`layout.py:67-70`), no `aria-current`, no underline; the `h1` is the only cue. The
  home link reads "Dash". "Your account" is reachable only from inside the gear, under
  a different name ("Password & sign-in"), and the login copy calls it a third
  (`login.py:500`).
- **The volunteer page prints custom-field values raw:** `PT3H30M` for a duration,
  `16:00:00` for a time, `2021-05-05 16:40:00` for a timestamp, a bare UUID; and ten
  `—` lines for empty fields ahead of the notes and the service hours.
- **The events page defaults to "My duties"** — for an administrator or a leader with
  no shifts that is an empty month grid above the fold with "Nothing you are signed
  up for this month." and the table below it.
- **A member on a page they cannot see gets one sentence and no way back**
  ("This event is visible to the members of its team."), and an admin page shows a
  non-admin "Admins only." (`guards.py:20`).
- **Dark mode is sound** except the two unremapped banners above; the calendar, the
  tables and the badges read well.
- **Two prominent copy defects:** "consulatative" in `IGNATIAN_NOTE`
  (`elections_page.py:40`) and a missing space in `STAR_NOTE` (`:46`, "score
  honestly.Individual votes").

### Written into the manual as workarounds

The guide is careful and honest, which is how it records the interface's debts:

- "On a phone the header hides your address; open a team page, click your own name,
  then click *Full profile*." (`tutorials/your-teams-and-your-service.md:45`)
- "Move the mouse over the badge. It changes to *invite to create account*." followed
  by "A small mail icon next to the badge marks it as a button. On a touch screen,
  tap it." (`tutorials/lead-a-team.md:42-54`) — a hover affordance patched with a hint.
- "The dialog closes and the page reloads… There is no message."
  (`how-to/edit-a-members-contact-details.md:40`)
- "After a removal, the row is gone at once. There is no confirmation step."
  (`how-to/add-or-remove-a-member.md:37`)
- "If the email did not arrive, the dialog was the one moment to copy the link."
  (`tutorials/administer-the-parish.md:31`)
- "If you forgot your password, sign out and sign in with an emailed code. Then come
  back here." (`how-to/change-your-password.md:9`) — no forgot-password link.
- "Scroll down to the graph… Scroll to the bottom… Scroll back up"
  (`tutorials/first-sign-in.md:45-47`) — the landing page needs choreography.
- "The site has no report button." (`how-to/report-a-problem.md:7`) — six steps to
  the mailto line at the foot of a manual page.

### What already works, and must survive every step

The month grid as a real `<table>` that becomes a list at 40 rem; the native
`popover` subscribe panel and `<meter>` fill bars (`05634ce`); the events listing's
URL-carried state (`events_page.py:116-142`, the cleanest state handling in the app);
`forms.py`'s three helpers; `column_order.py`; `account_status.py`'s five badge
states with exact tooltips; the workload page's live contrast readout
(`workload_admin_page.py:16-34`, the one inline validation in the app); the login
page's error copy; the WCAG AA contrast pass and its test; the "chrome, then what you
came for, then the plumbing" page order of `4a2603d`; the stat tiles; the search
box's stale-response guard (`search_box.py:97`).

## The steps

Every step keeps the existing names working, updates `screens.md` and the affected
how-to in the same commit, adds a NiceGUI `User` test (`tests/test_ui_*.py`) for the
control it adds or changes and a browser test only for what needs a browser
(keyboard, drag, axe), regenerates the screenshots and names in the commit body the
screens that changed, and ends with the full suite green in its own commit.

### Phase 0 — Evidence

- [x] **0. A screenshot harness.** `scripts/screenshots.py` (beside `scripts/bench.py`,
  same standing: a local tool, not CI) starts the app on a free port with the
  scheduler off and the NiceGUI storage in a temp dir, signs in through the real form
  as admin / leader / member / anonymous, and writes
  `screenshots/<role>/<page>@<width>[-dark].png` for every route in `screens.md` at
  1280 px and 390 px, light and dark, plus the dialogs (new event, edit volunteer,
  sign-up) and the side panel. `make screenshots`; `screenshots/` gitignored. The
  baseline is captured before step 1 and every later step regenerates it. The seed
  already has the scale cases (a 59-member team, 500 volunteers, 33 accounts; "Create
  accounts for all volunteers with email" makes ~400 more locally).

### Phase 1 — Safety and feedback

- [x] **1. Every removing action asks first.** `forms.confirm(danger=True)` at the
  eight sites in the table, with the wording rule from Decisions ("Delete the team
  Hospitality?" / detail: "Its 59 roster places go with it. The history keeps them."
  / yes "Delete the team"). The leader's "Remove" on a shift gets the same confirm
  (not the volunteer's reason dialog). A source sweep in `tests/test_ui_layer.py`
  lists the removing service functions (`teams.delete`, `memberships.remove`,
  `events.remove_assignment`, `events.delete_slot`, `users.reinvite`,
  `photos.remove`, `branding.remove_logo`) and asserts each GUI handler that calls
  one contains an `await confirm(`.
- [ ] **2. Dialogs with input do not vanish on a backdrop click.** `forms.dialog_card`
  gains `persistent=True` (Quasar `persistent`) as its default; `confirm` stays
  dismissible. Cancel is already on every `actions` row. The five widths collapse to
  two (`w-96`, `w-[32rem]`) and the seven hand-rolled button rows move to
  `forms.actions` (including the "Possible double booking" second dialog at
  `events_page.py:397`).
- [ ] **3. Notifications carry an icon and a message that outlives the reload.**
  `context.toast` and a new `context.success(msg)` pass `type=` (`positive` /
  `warning` / `negative`), refusals get `timeout=8000` and a close button. `frame()`
  pops `app.storage.user["flash"]` and shows it, so `run_command(reload=True)` stores
  its success line there instead of racing the reload. The guide's "There is no
  message." lines go.
- [ ] **4. A button that is working says so.** `forms.actions` and a new
  `widgets.busy(button)` wrap the handler: `loading` + `disable` while awaited,
  restored after (or the page reloads). Applied to every `actions` primary, to
  "Sync now", "Fetch now", "Overwrite sheet", "Apply this import", "Upload" and the
  bulk "Create and email invites". Replaces the "Syncing…" toast at
  `teams_page.py:531,710`.
- [ ] **5. Required fields say so before submit.** `ui.input(validation=…)` on
  required fields (marked in the label with " *" and a one-line "Required" rule), and
  handlers call `.validate()` before the command; the ~20 post-submit toasts become
  field errors with focus moved to the first failing field. Date/time fields keep
  their pickers and add a format rule.
- [ ] **6. Empty states offer the next thing.** `widgets.empty_state(text, *, action,
  href)` for: `/volunteers` with no hits ("Nobody matches 'xyz'." + Clear search;
  the band filter shown as a chip so it cannot stay applied invisibly), `/events`
  (the search box stays when the list is empty, `events_page.py:677-685`), a roster
  with nobody (the leader's add-member row sits under it), `/elections` for a leader
  with nothing open, `/admin/users` with no accounts.

### Phase 2 — Wayfinding

- [ ] **7. The header says where you are.** `frame()` reads the request path and
  sets `aria-current="page"` on the matching nav button (and menu item on the phone);
  `theme.css` underlines it in the accent. The home link reads "Dashboard" in the
  same Cinzel face ("Dash" survives nowhere else in the copy).
- [ ] **8. One account menu.** The address and the avatar become one menu button
  (avatar, or a person icon for an account with no photo): the reader's name, then
  *My profile* (linked volunteers), *Your account*, *Sign out*. The page title, the
  menu label and the login copy (`login.py:500`) all say "Your account". On the phone
  the name is inside the menu, so the three-hop workaround in the tutorial is deleted.
- [ ] **9. A refusal has a way back.** `guards.deny_unless_admin` and the four
  "visible to…" sentences (`events_page.py:1649`, `elections_page.py:216,682`,
  `teams_page.py:973`) render `widgets.denied(reason, back=(label, href))`; a
  not-found profile or event likewise.
- [ ] **10. Section titles are headings.** `a11y.heading(text, level=2)` (and 3 in
  cards) replaces the 35 `text-lg font-medium` labels; the login and invite pages get
  an `h1`. A source sweep forbids the class pair on a `ui.label` outside `forms.py`.
- [ ] **11. Help for this page.** `frame(title, actor, help="lead-a-team")` draws a
  small `?` icon button at the end of the title row that opens the manual page for
  this screen in a new tab (`help_links.py` already maps topics). Every framed page
  passes a slug. The dashboard's Guides band shrinks in step 25 because of this.
- [ ] **12. A snapshot does not silently end.** While `as_of` is set, the header's
  links to the pages that support it (`/`, `/teams`, `/teams/{id}`) carry the
  parameter (`layout.py:35-46`), and the as-of banner says which pages show today
  ("Volunteers, Events and Elections show today"). The panel's *Full profile* keeps
  its live-only note but says so in the button's tooltip.

### Phase 3 — Words for dates and values

- [ ] **13. One short form for tables and badges.** `timefmt.when_short(dt, tz)` →
  "Thu, Sep 10, 7:30 PM"; `timefmt.day(dt, tz)` → "Sep 10, 2026"; both in the parish
  zone like `event_when`. Replaces the seven page-level formats in the table above
  (events table "When", the calendar's `%H:%M`, the dashboard's "next", the account
  page's pending-change line, election deadlines "Nominating until Sep 13", last
  login, sync lines, invites, the public ministry pages). A source sweep forbids
  `%Y-%m-%d` and `%H:%M` format strings in `ui/` outside `date_input.py`.
- [ ] **14. Custom fields read by type.** `fieldcodec.display(field, value)`: a
  duration as "3 h 30 min", a time as "4:00 PM", timestamps through `timefmt`, a
  checkbox as "yes"/"no", a UUID in monospace. Used on the profile, the panel and the
  volunteers table. Unset fields collapse into one muted line ("Not recorded: T-shirt
  size, Years in the parish, …") so a core member still sees what to fill without ten
  `—` rows.
- [ ] **15. Copy.** "consulatative" → "consultative"; the missing space in
  `STAR_NOTE`; "Password & sign-in" → "Your account" everywhere; the two "Change"
  buttons eleven lines apart on the team page (`teams_page.py:459-463`) get their
  objects ("Change the doc", "Change the spreadsheet").

### Phase 4 — The long pages

- [ ] **16. The roster is a table.** `ui.table` with Name (the panel button, as on
  `/volunteers`), Role (a badge; for leaders a click opens one small role dialog —
  one component on demand instead of a `QSelect` per row), Email, Phone, Account
  (the `account_status` badge), Since; sortable; `wire_search` over it; 25 per page
  with "All"; the invite control (step 27) and the confirmed remove in the last cell.
  Below 40 rem, Email and Phone columns hide (column `classes`/`headerClasses` +
  `theme.css`) — they are in the panel a tap away. "Copy email list", "Email all"
  and the export stay in the action row.
- [ ] **17. Accounts is a table.** Search over address and name, sort, 25 per page,
  the badge column, and a row-end "⋯" menu with the four actions named in words
  ("Change linked volunteer", "Make admin", "Disable", "New invite link (resets the
  password)") instead of four icons; "invite pending" opens the dialog it does now.
- [ ] **18. Team weights are a table.** Grouped by top-level team, searchable, the
  label cell full-width on the phone (`w-96` today), "Save weights" sticky at the
  bottom of the card. The band editor gains a one-line note that saving recolours the
  volunteers list, the graph and the dashboard chips.
- [ ] **19. The team page folds its plumbing.** After the roster, "Roster
  spreadsheet", "Import a .csv" and "Volunteer home page" become `ui.expansion`
  panels, closed unless something is linked or the last sync failed. Sub-teams stay
  above the roster (`8383242`); the page still reads chrome → roster → plumbing
  (`4a2603d`). At 1280 px the 59-member page drops from 5,289 px to roughly the
  height of 25 rows.
- [ ] **20. Tables on the phone show what a phone can show.** Events: below 40 rem
  the row is When (short form) + Event, with team and location as a second muted
  line in the Event cell and *serving* as a badge; Team, Location, You hide.
  Volunteers: Name + one muted details line (email · phone); custom-field columns
  hide below md. Teams: the counts hide below 40 rem, Gaps stays. Done with column
  classes and `theme.css`, so `column_order` keeps working.

### Phase 5 — Fewer reloads, bulk saves

- [ ] **21. Four sections refresh in place.** The roster (`teams_page.py:968`), a
  slot card's assignments (`events_page.py:1533`), attendance (`:1586`) and the
  ballot + voters (`elections_page.py:562,603`) become `@ui.refreshable` builders
  that load inside `page_ctx()` and render after it (so `test_ui_layer` still
  passes), and their commands run `reload=False` with `on_ok` calling `.refresh()`.
  Verify against nicegui 3.14's `refreshable.py` that `refresh()` re-renders only the
  calling client's targets, since a module-level refreshable is shared by every open
  page; if not, key the builder per client. Scroll, sort, search and the toast now
  survive a role change, an assignment, a tick, a score.
- [ ] **22. Bulk where the leader works in bulk.** Attendance gets "Save all" (one
  command over the changed rows, the diff-against-originals pattern of
  `workload_admin_page.py:150-173`) beside the per-row Save. "Schedule someone"
  becomes a multi-select with the *available* people first; one command assigns them
  all in one transaction. "Take this slot" (`events_page.py:569`) gets a one-line
  confirm — it is a commitment, and signing up for the same slot from the event page
  takes a dialog.
- [ ] **23. Listings carry their state in the URL.** `/volunteers` carries `?q=` and
  `?band=`; `/teams` carries `?q=`; the search boxes read their initial value from
  the URL, as `/events` already does with `Listing`. A reload lands where the reader
  was.

### Phase 6 — The dashboard

- [ ] **24. The graph is one click away.** It renders inside a closed
  `ui.expansion("Ministry graph")` at the foot of the page, so the 300-iteration
  layout (`cytoscape_graph.js:220-237`) runs only when opened; the caption is rewritten
  for touch ("tap a node"). The focus select and fit button move inside it. On the
  phone the same. The "Active teams" tile and the team page keep their links to it.
- [ ] **25. Guides are three, not forty.** The band shows the tutorial and the two
  how-tos for the reader's highest role, plus "All guides" (the manual's sidebar). The
  rest is a `?` away on each page (step 11).
- [ ] **26. The reader first.** For a linked volunteer the order is My service, My
  teams, Needs attention, Parish; an administrator with no volunteer record sees
  Parish, Needs attention. The events page opens on "Whole parish" for a reader with
  no upcoming duties and on "My duties" otherwise (the choice stays in the URL).

### Phase 7 — The phone

- [ ] **27. Invite is a button, not a hover.** The badge stays informational; beside
  it a small outlined "Invite" / "Re-invite" button appears where `invitable()` says
  so. `.vdb-invite-swap` and its 60 lines of CSS go; the tutorial's "move the mouse
  over the badge" and its touch apology go with them.
- [ ] **28. Targets are 44 px on touch.** `@media (pointer: coarse)` gives dense icon
  buttons and the role badge/button the padding to reach 44 px (WCAG 2.5.8 at AA is
  24 px; 44 px is the AAA and iOS figure, and the readers are not all young).
- [ ] **29. Dialog field rows stack below 40 rem.** The date/start/end row
  (`events_page.py:414-417,918-925`) and the availability note + buttons row
  (`:1409-1426`) wrap to full-width fields on the phone.
- [ ] **30. Forgot password, on the login card.** A "Forgot your password?" link that
  does what the guide describes (submit with the password blank → emailed code), so
  the reader does not have to know the trick.

### Phase 8 — Theme and print

- [ ] **31. Dark follows the OS until the reader chooses.** `theme.py:8` stores
  `None` until the switch is used; `ui.dark_mode(None)` is Quasar's auto mode. The
  anti-FOUC style adapts; `test_browser_session`'s "persists across sign-out" keeps
  passing for an explicit choice.
- [ ] **32. A print stylesheet.** `@media print` in `theme.css`: header, nav, gear,
  search, action rows, graph and drawer hidden; white ground, black ink, tables with
  rules; the team roster and an event page print as the sheet a leader pins up.
- [ ] **33. The four unremapped tints become tokens.** `--vdb-warn-bg/-ink` and
  `--vdb-crit-bg/-ink` in both modes for the mail-quota banner
  (`layout.py:128-143`) and the pending-email line (`account_page.py:255`);
  `test_theme_contrast.py` gains a sweep asserting every Tailwind tint class used in
  `ui/` is remapped for dark in `theme.css`.
- [ ] **34. (Optional, Ben's call) A self-hosted body serif.** `--vdb-serif` falls
  through to a generic serif on Linux and Android, so the site reads differently per
  device. One libre Palatino-alike (TeX Gyre Pagella, ~60 KB woff2, latin subset)
  preloaded like Cinzel would make the face the same everywhere. Yes, do this.

### Phase 9 — Accessibility, round two

- [ ] **35. The search box is a combobox.** `role="combobox"`, `aria-expanded`,
  `aria-controls`, and arrow keys that move an `aria-activedescendant` highlight
  through the suggestion list (`search_box.py:56-62` creates the menu `no-focus`, so
  today only the mouse reaches it). Enter opens the highlighted item. If the
  hand-rolled keyboard handling grows past ~60 lines, switch to a `QSelect
  use-input` with an `option` slot for the Teams/Volunteers headers and let Quasar
  own the ARIA — decide at implementation.
- [ ] **36. Counts and reports are announced.** `aria-live="polite"` on the count
  label `wire_search` updates (`tables.py:63`) and on the CSV import report.
- [ ] **37. Clickable text is a button.** `volunteer_link` (`volunteer_panel.py:49`),
  the clickable badges (`invites.py:275`, `admin_page.py:239`), the photo
  (`photo_dialog.py:144`) and the logo (`logo_dialog.py:131`) become real buttons
  (`.vdb-rowbtn` already styles a bare `<button>`) with `aria-label`s; Enter and
  Space work.
- [ ] **38. axe fails on moderate.** `tests/e2e/test_browser_a11y.py:19` adds
  `moderate` to the failing impacts and `/teams/15`, `/events/{id}`,
  `/elections/{id}` to the page list once steps 10, 35–37 have landed. The four
  deferrals in `accessibility.md:116-126` stay deferred except the menu-items-as-
  links one, which step 7's `aria-current` work touches anyway.

## Working rules

- One step, one commit, on `main`, with the checkbox ticked in the same commit and
  the commit body naming any behaviour change and the screens whose screenshots
  changed. Stage explicit paths, never `git add -A`.
- `screens.md` and the affected how-to change in the same commit as the control. The
  docs are at a zero STE baseline: run `uv run pytest -m pure -q tests/test_docs_style.py`
  before committing.
- A NiceGUI `User` test (`tests/test_ui_*.py`, the `user_simulation` fixture) for every
  new or changed control; a browser test (`tests/e2e/`) only for keyboard, drag, axe
  or CSS behaviour the simulation cannot see.
- Verify visual work by screenshot (`make screenshots`), not by reading the code.
- The `ty` ceiling in `scripts/typecheck.py` may only go down; the contrast test and
  the CSS-invariants test must pass unchanged unless a step says it extends them.
- Never two Postgres pytest sessions at once; the dev server runs `python -m
  volunteerdb.main` under reload.
- Names in the copy follow `docs/guide/reference/words.md`; a new word goes there
  first.

## Verification

Per step:

```sh
make types                                  # ty at or under CEILING
uv run pytest -m pure -q                    # sweeps, contrast, STE, CSS invariants
make test                                   # full suite incl. tests/e2e (needs podman db)
make screenshots && git status screenshots/ # regenerated; eyeball the changed screens
```

End to end, after each phase: the `verify` skill's launcher (`.claude/skills/verify`)
as admin, leader (`maria.alvarez@example.org`) and member (`felix.garcia@example.org`)
through the flows the phase touched — add a member, remove one, assign a shift, mark
attendance, vote — at 1280 px and at 390 px in the browser's device mode, light and
dark; `axe` via `uv run pytest tests/e2e/test_browser_a11y.py`. Ben's read of the
regenerated screenshots is the acceptance for every phase.

## Out of scope

The public `/ministries` shell (its own light-only CSS in `ministries_routes.py:30-51`;
small, and the parish's Google Docs decide most of what is on it); the eleven-step
Google Sheets and Docs round trips (they happen in Google); keyboard reordering of
table columns (deferred in `accessibility.md`, still a preference); a different
visualisation in place of the graph; the JSON API; mail templates; a "report a
problem" button (the manual's mailto line is a policy question — who receives it —
before it is a UI question); new features such as recurring-event editing or a
volunteer's own availability calendar.

