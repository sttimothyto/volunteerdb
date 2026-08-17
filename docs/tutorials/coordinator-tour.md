# A coordinator's first session

This tour walks through VolunteerDB the way a ministry coordinator uses it,
signed in as **Maria Alvarez** — the demo parish's busiest volunteer: leader
of *Liturgy* and *Altar Society*, helper in *Hospitality*. You need a
[seeded development instance](install-and-run.md) at `http://localhost:8080`
(on the real site, everything works the same with your own account).

## 1. Sign in without a password

1. Open the app; you land on the sign-in page.
2. Enter `maria.alvarez@example.org` and leave the password field
   **empty**, then submit.
3. The page switches to a code prompt: a 6-digit sign-in code has been
   emailed. On a dev instance there is no real email — the code is printed
   in the terminal running the app, in the logged message body.
4. Type the code and press **Sign in with code**. You're in.

That is the everyday login for volunteers who don't want a password. (Maria
also has one — `volunteer` — and either works; the code expires after 10
minutes.)

## 2. Read the dashboard

- **My teams** lists Maria's three ministries with her role in each.
- The **ministry graph** shows the whole parish as a network — more on it
  in step 7.
- The **search box** finds any volunteer or team — try `felix`. Matches appear
  in a dropdown as you type (from two letters on): pick a team to open its
  page, a volunteer to open their side panel, or press Enter for the full
  result list.
- Header → **Elections**: **Vacancies** shows leadership gaps — but only in
  the teams *you* lead. Maria's ministries are fully staffed, so her page
  is quiet; an administrator would see *Hospitality* flagged there (the
  demo team seeded without a leader) and could **start a proposal** for the
  seat: candidates are nominated (each with a "why them?" note and their
  current commitments in plain view) until a nomination deadline, a voting
  roll — the team's leadership and core plus the **Clergy** team, who vote
  on every seat in the parish — scores them
  by [STAR voting](../explanation/elections.md) until a voting deadline, and
  only then does a manager appoint someone (creating the membership) or
  send the question around again.

## 3. Open a team and manage its roster

Header → **Teams** → *Liturgy*. Because Maria leads it:

- The roster shows every member **with contact details** and their role.
  Click anyone to open their side panel: because Maria leads their team,
  it carries a [workload](../explanation/workload.md) badge.
- Each row ends with the member's sign-in status: an **account** badge (or
  **no account** for someone who has never been registered) and the date
  they last logged in. That is who can be reached through the app, as
  opposed to who is merely on the list.
- Sub-teams (*Altar Servers*, *Lectors*, *Music Ministry*, *Sacristans*)
  are listed — Maria's leadership covers them too.
- **Add a member:** pick any volunteer, role *Member*. **Change a role:**
  promote someone to *Core team member*. Both take effect immediately.
- Undo your experiments if you like — or keep them and see step 4 remember
  the old state.

## 4. Look at the past

On the team page, click the **settings gear** in the header for the **"View
as of"** date picker and choose a date last year. An amber banner appears:
you are reading a snapshot — the roster as it actually was, including your
pre-experiment state from step 3, and people who have since left. **Back to
now** in the banner returns you to the present. Every
change you make is preserved this way, with who-changed-what recorded.

## 5. Export a roster

Still on the team page, **Export roster (.csv)** downloads the team's
roster — handy for a printed phone list. What lands in the file respects
your permissions: it contains contact details because Maria may see them.

## 6. Read a volunteer in depth

Open **Peter Kowalski** from the Altar Servers roster:

- His profile shows contact info, custom fields (e.g. *Safeguarding
  training*), **Last login**, and which teams he serves on.
- The **timeline chart** shows his service history — note the two-color bar
  on Altar Servers: he served as a plain member before taking over as
  leader.
- The **impact report** answers "if Peter leaves, what holes appear?" —
  Altar Servers would lose its leader, Youth Group its second.

This is the app's core question, answerable for anyone, as of any date.

## 7. See the whole parish as a graph

Back on the **Dashboard**: volunteers and teams as a network, gold edges
marking leadership, dots colored by workload band where you may see it.
Use the team filter to focus on *Liturgy* and click a team node to jump to
its page. Maria's own dot stays grey — nobody sees their own workload —
but an administrator would see it red: three weighted ministries, two of
them led. That red dot is *why* workload exists: it's what the next "could
you also…?" conversation should know.

## 8. Know what you can't see

Sign out (header menu) and sign in as `felix.garcia@example.org` /
`volunteer` — a plain member of Altar Servers and Youth Group. The same
pages now show less: rosters list names but contact details are `•••`, no
workload badges, no Elections page, no roster editing. Same data, same
pages — access follows
the fourfold role, as laid out in the
[permission matrix](../reference/permissions.md#permission-matrix).

## Where next

- Everyday tasks: [manage accounts](../how-to/manage-users.md),
  [import/export spreadsheets](../how-to/import-export.md).
- The ideas behind what you just saw:
  [permissions](../explanation/permissions.md),
  [history](../explanation/history.md),
  [workload](../explanation/workload.md).
