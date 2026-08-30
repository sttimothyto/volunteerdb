# VolunteerDB

VolunteerDB is the parish's list of volunteers and ministry teams. It shows
who serves on which team, in which role, and who fills each shift. It keeps
the history, so the parish can look back at any date.

## Where do I start?

- **New here?** Follow [Sign in for the first time](guide/tutorials/first-sign-in.md).
  It takes about 10 minutes.
- **On a team?** [Your teams and your service](guide/tutorials/your-teams-and-your-service.md)
  shows you your teams, your profile and your photo.
  [Your first shift](guide/tutorials/your-first-shift.md) shows you the events
  and the calendar.
- **Do you lead a team?** [Lead a team](guide/tutorials/lead-a-team.md) takes
  you through the roster, the spreadsheet and the home page.
- **Do you administer the site?** Start with
  [Administer the parish](guide/tutorials/administer-the-parish.md).
- **Do you want to get one thing done?** The sidebar sorts the how-to guides
  by who can do what: everyone, team members and voters, core members,
  leaders and seconds, administrators.
- **Do you want to look something up?** [The screens](guide/reference/screens.md),
  [The roles](guide/reference/roles.md),
  [The emails the site sends](guide/reference/emails.md) and
  [Words](guide/reference/words.md).
- **Do you want to know why?** The explanation pages start with
  [Why VolunteerDB](guide/explanation/why-volunteerdb.md).

Type a question in the search box in the sidebar. The results come from
these pages.

## Do you run or develop this site?

Turn on *Are you a developer?* in the sidebar. The technical manual then
appears below the user guide: installation, deployment, backups, the HTTP
API, the database schema, and the design decisions behind them. The switch
works on any page, and your browser remembers the choice.

The technical manual is behind the sign-in. Sign in to the site first, and
then turn on the switch. The user guide itself is open to everybody.

VolunteerDB answers one question for a transient parish: when this
parishioner leaves, what holes do I have to fill? It tracks about 500
volunteers across about 50 ministry teams, with sub-teams and 4 roles per
team. A web interface (NiceGUI) and a JSON API (FastAPI) run in one process
over one PostgreSQL database, with full history.

Quick links for developers: the interactive API description at `/docs` on a
running instance, and the terse quick start in the repository's `README.md`.

```{toctree}
:hidden:
:caption: Tutorials

guide/tutorials/first-sign-in
guide/tutorials/your-teams-and-your-service
guide/tutorials/your-first-shift
guide/tutorials/lead-a-team
guide/tutorials/administer-the-parish
```

```{toctree}
:hidden:
:caption: How-to: everyone

guide/how-to/sign-in-with-a-code
guide/how-to/change-your-password
guide/how-to/change-your-email-address
guide/how-to/update-your-contact-details
guide/how-to/add-your-photo
guide/how-to/calendar-on-your-phone
guide/how-to/find-a-volunteer-or-a-team
guide/how-to/see-the-parish-as-of-a-date
guide/how-to/dark-mode
guide/how-to/report-a-problem
```

```{toctree}
:hidden:
:caption: How-to: team members and voters

guide/how-to/vote-in-an-election
guide/how-to/ask-for-a-substitute
guide/how-to/cover-a-shift
```

```{toctree}
:hidden:
:caption: How-to: core members

guide/how-to/read-the-full-roster
guide/how-to/invite-a-volunteer
```

```{toctree}
:hidden:
:caption: How-to: leaders and seconds

guide/how-to/add-or-remove-a-member
guide/how-to/change-a-members-role
guide/how-to/edit-a-members-contact-details
guide/how-to/link-a-roster-spreadsheet
guide/how-to/import-a-csv
guide/how-to/export-the-roster
guide/how-to/publish-the-team-home-page
guide/how-to/create-an-event
guide/how-to/cancel-an-event
guide/how-to/hand-over-or-withdraw-a-shift
guide/how-to/run-an-election
guide/how-to/read-workload
guide/how-to/create-a-task-force
```

```{toctree}
:hidden:
:caption: How-to: administrators

guide/how-to/manage-accounts
guide/how-to/resend-an-invite
guide/how-to/add-a-custom-field
guide/how-to/set-workload-bands
guide/how-to/upload-the-parish-logo
guide/how-to/read-the-audit-log
```

```{toctree}
:hidden:
:caption: Reference

guide/reference/screens
guide/reference/roles
guide/reference/emails
guide/reference/roster-spreadsheet
guide/reference/words
```

```{toctree}
:hidden:
:caption: Explanation

guide/explanation/why-volunteerdb
guide/explanation/who-can-see-what
guide/explanation/history-and-as-of
guide/explanation/elections
guide/explanation/sign-in-without-a-password
guide/explanation/spreadsheets-and-home-pages
```

```{toctree}
:hidden:
:caption: Technical tutorials

tutorials/install-and-run
tutorials/coordinator-tour
```

```{toctree}
:hidden:
:caption: Technical how-to guides

how-to/new-instance
how-to/deploy
how-to/backup-restore
how-to/rotate-secrets
how-to/manage-users
how-to/roster-spreadsheets
how-to/team-home-pages
how-to/google-calendar-sync
how-to/custom-fields-and-workload
how-to/site-logo
how-to/api-recipes
how-to/audit-logs
how-to/write-a-migration
how-to/run-tests
```

```{toctree}
:hidden:
:caption: Technical explanation

explanation/architecture
explanation/permissions
explanation/history
explanation/auth
explanation/workload
explanation/elections
explanation/events
explanation/deployment
explanation/accessibility
explanation/writing-style
```

```{toctree}
:hidden:
:caption: Technical reference

reference/configuration
reference/site-config
reference/http-api
reference/cli
reference/schema
reference/permissions
reference/spreadsheets
reference/query-language
reference/glossary
```
