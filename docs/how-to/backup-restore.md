# Back up and restore the database

All state that matters lives in PostgreSQL (the only other persistent app
state is the NiceGUI storage volume, which holds nothing that cannot be
regenerated). A plain-SQL `pg_dump` is a complete backup, including the
history twins.

## Back up

Production (Quadlet container on the server):

```sh
ssh sttimothyto-prod \
  'podman exec volunteerdb-db pg_dump -U volunteerdb volunteerdb' \
  > vdb-backup-$(date +%F).sql
```

Development (compose container; find its name with `podman ps`):

```sh
podman exec <db-container> pg_dump -U volunteerdb volunteerdb > backup.sql
```

:::{warning}
There is **no scheduled backup** yet — dumps are manual. Until that gap is
closed, a host cron entry is the simplest fix, e.g. in `root`'s crontab on
the server:

```
15 2 * * * podman exec volunteerdb-db pg_dump -U volunteerdb volunteerdb | gzip > /var/backups/volunteerdb-$(date +\%F).sql.gz
```

plus off-host copying and retention of your choice.
:::

## Restore

Restore into an **empty** database. For a fresh start, drop and recreate
first (this destroys current data — be sure):

```sh
podman exec -i <db-container> psql -U volunteerdb -d postgres \
  -c 'DROP DATABASE volunteerdb;' -c 'CREATE DATABASE volunteerdb;'
podman exec -i <db-container> psql -U volunteerdb -q -d volunteerdb -v ON_ERROR_STOP=1 \
  --single-transaction < backup.sql
```

In production, stop the app first so nothing writes mid-restore, and start
it again afterwards:

```sh
systemctl stop volunteerdb-app
# … restore as above …
systemctl start volunteerdb-app
```

## Verify

The dump contains the schema, so no migration run is needed. Check row
counts and history:

```sh
podman exec <db-container> psql -U volunteerdb -d volunteerdb -c \
  'SELECT (SELECT count(*) FROM volunteer) AS volunteers,
          (SELECT count(*) FROM membership) AS memberships,
          (SELECT count(*) FROM membership_history) AS history_rows;'
```

then sign in and spot-check a team page, including a "View as of" date
before the backup was taken.
