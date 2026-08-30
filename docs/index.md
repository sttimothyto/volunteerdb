# VolunteerDB

VolunteerDB answers one question for a transient parish: **"when this
parishioner leaves, what holes do I have to fill?"** It tracks ~500 volunteers
across ~50 ministry teams (with sub-teams), each volunteer holding one of four
roles per team. A server-rendered web GUI (NiceGUI) and a JSON API (FastAPI)
run in one process against one PostgreSQL database, with full history: any
team, volunteer, or the whole ministry graph can be viewed *as of any past
date*.

## Where do I start?

- **New to the project?** Follow the [installation tutorial](tutorials/install-and-run.md)
  to get a seeded development instance running, or take the
  [coordinator's tour](tutorials/coordinator-tour.md) of the web interface.
- **Standing up your own instance?** [Stand up a new
  instance](how-to/new-instance.md) is the ordered checklist, from DNS to the
  first backup.
- **Need to get something done?** The how-to guides are task recipes —
  for example [deploying to production](how-to/deploy.md) or
  [importing a spreadsheet](how-to/roster-spreadsheets.md).
- **Want to understand a design?** The explanation pages cover the
  [architecture](explanation/architecture.md), the
  [permission model](explanation/permissions.md), and the
  [history mechanism](explanation/history.md), among others.
- **Looking something up?** The reference pages are lookup tables:
  [configuration variables](reference/configuration.md),
  [HTTP API endpoints](reference/http-api.md), the
  [database schema](reference/schema.md), and more.

Quick links: this documentation at `/manual` on any running instance (signed
in) · interactive OpenAPI docs at `/docs` · terse quick start in the
repository `README.md`.

```{toctree}
:hidden:
:caption: Tutorials

tutorials/install-and-run
tutorials/coordinator-tour
```

```{toctree}
:hidden:
:caption: How-to guides

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
:caption: Explanation

explanation/architecture
explanation/permissions
explanation/history
explanation/auth
explanation/workload
explanation/elections
explanation/events
explanation/deployment
explanation/accessibility
```

```{toctree}
:hidden:
:caption: Reference

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
