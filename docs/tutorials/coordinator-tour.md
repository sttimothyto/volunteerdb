# A coordinator's first session

This tour walks through VolunteerDB the way a ministry coordinator uses it.
You sign in as **Maria Alvarez**, the demo parish's busiest volunteer:
leader of *Liturgy* and *Altar Society*, helper in *Hospitality*.

You need a [seeded development instance](install-and-run.md) at
`http://localhost:8080`. On the real site, everything works the same with
your own account.

## 1. Sign in without a password

1. Open the app. You land on the sign-in page.
2. Enter `maria.alvarez@example.org`.
3. Leave the password field **empty** and click *Sign in*.
4. You see a code prompt. The site has emailed a 6-digit sign-in code. On a
   dev instance there is no real email. The terminal that runs the app
   prints the code, in the logged message body.
5. Type the code and click *Sign in with code*. You are in.

That is the everyday login for volunteers who do not want a password.

- Maria also has a password, `demo`, and either works.
- The code expires after 10 minutes.

## 2. Read the dashboard

The page runs from the widest audience to the narrowest:

- what the parish looks like
- what the people who run ministries must act on
- what is only about Maria: her teams and her own service
- the ministry graph
- the *Guides* band, last of all

A section you have no right to is not there at all. Maria is a leader, not
an administrator, so her page opens on *Needs attention*, and the
parish-wide figures above it never appear.

- *Needs attention* counts the teams Maria helps run, the people on them,
  and the ones with no email address on file. The app cannot invite those
  people or mail them about an event. Because she *leads* Liturgy, she also gets:
  - the leadership gaps: teams with no leader or no second, worst first, and
    clickable
  - the workload spread of her people
  - any shift short of volunteers in the next 30 days
  - any seat with an election in progress

  A core team member would see the first 3 tiles and none of the rest: they
  read rosters, they do not run the teams.
- *My teams* lists her 3 ministries with her role in each. A row opens the
  team's page.
- *My service* is Maria's own: her next duty, shifts she could cover, a
  ballot that waits on her, her hours. Her own workload band is not here and
  never will be (see step 7).
- The **ministry graph**, below all of these, shows the whole parish as a
  network. Step 7 has more on it.
- The **search box** finds any volunteer or team. Try `felix`. Matches
  appear in a dropdown as you type, from 2 letters on. Pick a team to open
  its page, or a volunteer to open their side panel. Press Enter for the
  full result list.
- The *Guides* band, at the foot of the page, links to the short pages of
  the user guide. Each link opens in a new tab. The groups grow with what
  you can do: Maria, a leader, sees *For leaders and seconds*; an
  administrator also sees *For administrators*.
- Header → *Elections*: *Vacancies* shows leadership gaps, but only in the
  teams *you* lead. Maria's ministries are fully staffed, so her page is
  quiet. An administrator would see *Hospitality* flagged there (the demo
  team seeded without a leader) and could click *Create proposal* for the
  seat. An election then runs like this:
  - People nominate candidates until a nomination deadline. Each nomination
    carries a "why them?" note and the candidate's current commitments, in
    plain view.
  - A voting roll scores the candidates by
    [STAR voting](../explanation/elections.md) until a voting deadline. The
    roll is the team's leadership and core, plus the **Clergy** team, who
    vote on every seat in the parish.
  - Only then does a manager appoint someone (which creates the membership)
    or send the question around again.

## 3. Open a team and manage its roster

Header → *Teams* → *Liturgy*. Because Maria leads it:

- The roster shows every member **with contact details** and their role.
  Click anyone to open their side panel. Because Maria leads their team, the
  panel carries a [workload](../explanation/workload.md) badge.
- Each row ends with the member's sign-in status. It shows an *account*
  badge, or *no account* for someone who was never registered, and the date
  of their last login. That tells you who the app can reach, as opposed to
  who is merely on the list.
- Hover a *no account* badge. Because Maria leads the team, it becomes
  *invite to create account*. Click it and confirm the address. VolunteerDB
  creates the account and emails them a setup link; no admin is needed. The
  row then reads *invite sent*. Click that to bring the link back up, in
  case you would rather hand it over in person.
- The page lists the sub-teams (*Altar Servers*, *Lectors*, *Music
  Ministry*, *Sacristans*). Maria's leadership covers them too.
- **Add a member:** click *Add member*, pick any volunteer, and choose the
  role *Member*. **Change a role:** promote someone to *Core team member*.
  Both take effect at once.
- Undo your experiments if you like, or keep them and see step 4 remember
  the old state.

## 4. Look at the past

1. On the team page, click the **settings gear** in the header.
2. In the *View as of (YYYY-MM-DD)* date picker, choose a date last year.
3. You see an amber banner: you read a snapshot. It shows the roster as it
   was: your pre-experiment state from step 3, and the people who left
   since then.
4. Click *Back to now* in the banner to return to the present.

The site preserves every change you make this way, and records who changed
what.

## 5. Export a roster

Still on the team page, click *Export roster (.csv)*.

- It downloads the team's roster, handy for a printed phone list.
- What lands in the file respects your permissions: it contains contact
  details because Maria can see them.

## 6. Read a volunteer in depth

Open **Peter Kowalski** from the Altar Servers roster:

- His profile shows contact info, custom fields (for example *Safeguarding
  training*), *Last login*, and the teams he serves on.
- The *Service timeline* chart shows his service history. Note the two-color
  bar on Altar Servers: he served as a plain member before he took over as
  leader.
- The impact report, *If they leave, what vacancies appear?*, answers that
  question for Peter: Altar Servers would lose its leader, Youth Group its
  second.

This is the app's core question. It has an answer for anyone, as of any
date.

## 7. See the whole parish as a graph

Back on the *Dashboard*, the graph shows volunteers and teams as a network.
Gold edges mark leadership. Workload bands color the dots, where you can see
them.

Two gestures make a parish-sized map readable:

- **Zoom in**, and the names appear. They stay hidden further out, where 300
  of them would only overlap each other.
- **Hover any node**, and everything outside its immediate connections dims
  away. Hover a team to read its roster; hover a person to see every
  ministry they serve, at any zoom.

Size means something too:

- The bigger the bubble, the more ministries that person holds.
- The bigger the plaque, the larger the team.

1. Use the *Focus on team* filter to focus on *Liturgy*.
2. Click a team node to jump to its page.

Maria's own dot stays grey: nobody sees their own workload. An administrator
would see it red: 3 weighted ministries, 2 of them led. That red dot is
*why* workload exists. It is what the next "could you also…?" conversation
must know.

## 8. Know what you can't see

1. Sign out (the exit icon at the right end of the header).
2. Sign in as `felix.garcia@example.org` / `demo`, a plain member of Altar
   Servers and Youth Group.

The same pages now show less:

- Rosters list names, but contact details are `•••`.
- No workload badges.
- No *Elections* page.
- No roster edits.

Same data, same pages. Access follows the fourfold role, as the
[permission matrix](../reference/permissions.md#permission-matrix) lays it
out.

## Where next

- Everyday tasks: [manage accounts](../how-to/manage-users.md),
  [import/export spreadsheets](../how-to/roster-spreadsheets.md).
- The ideas behind what you just saw:
  [permissions](../explanation/permissions.md),
  [history](../explanation/history.md),
  [workload](../explanation/workload.md).
