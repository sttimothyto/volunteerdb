# Your first development instance

In about ten minutes you will have VolunteerDB running locally with a
seeded demo parish, sign in as its administrator, and prove the
installation with the test suite.

**You need:** Linux with [uv](https://docs.astral.sh/uv/) and podman
(`sudo apt install podman podman-compose`) installed, and a clone of this
repository as your working directory.

:::{tip}
`make seed` performs steps 1–4 in one command, and `make dev` does step 5.
This tutorial spells them out so you know what each one does; the targets are
one-line wrappers over exactly these commands.
:::

## 1. Create your environment file

```sh
cp .env.example .env
```

Everything can stay at its default for development. `VDB_STORAGE_SECRET` may
stay empty — the app then generates a random key each boot, which only means
your session ends when you restart it. Set it to a long random string
(`python3 -c "import secrets; print(secrets.token_hex(32))"` makes one) to stay
signed in across restarts. `make seed` writes the file with a generated secret
if it does not exist, and leaves an existing one alone.

## 2. Start PostgreSQL

```sh
podman compose up -d db
```

This runs PostgreSQL 17 in a container with a persistent volume, published
on `127.0.0.1:5432`. Success looks like `podman ps` showing the db
container with status *healthy* after a few seconds.

## 3. Create the schema

```sh
uv run alembic upgrade head
```

The first `uv run` also creates the project's virtualenv. Expected output:
a short `INFO` line per migration, ending at the newest revision, and no
traceback.

## 4. Seed the demo parish

```sh
uv run python scripts/seed.py
```

This loads 17 teams, 33 volunteers, membership history for the timeline
features, two custom fields, and four logins. It ends by printing:

```
  Clergy team filled — its members join every proposal's voting roll
  admin login:  admin@sttimothy.example / changeme
  leader login: maria.alvarez@example.org / volunteer
  member login: felix.garcia@example.org / volunteer
  clergy login: dominic.ferraro@example.org / volunteer
```

The clergy login is the useful one for trying out planning: those three
volunteers sit on *every* proposal's voting roll, so Fr. Dominic can score a
ballot on any seat in the parish.

(The script refuses to run on a non-empty database — that's it working as
designed.)

## 5. Run the app and sign in

```sh
uv run volunteerdb
```

Open <http://localhost:8080>. You are redirected to the login page; sign in
as `admin@sttimothy.example` with password `changeme` (leave "Keep me
signed in" ticked if you like — that's the 90-day session).

## 6. Look around

You land on the **Dashboard**. Three things confirm the seed did its job:

- Header → **Planning**: **Vacancies** lists *Hospitality* — the demo team
  seeded without a leader.
- Search for `maria` in the quick-search box and open **Maria Alvarez**:
  she leads two ministries, her workload badge is deep in the red, and her
  timeline chart shows an ended Youth Group spell.
- Header → **Teams** → *Liturgy*: a team with sub-teams and a roster you
  can manage; try the "View as of" date picker with last year's date and
  note the amber read-only banner.

Because `.env` has no `VDB_SMTP2GO_API_KEY`, any email the app "sends"
(try signing out and logging in as Maria with a blank password — the OTP
path) is printed to the terminal running `uv run volunteerdb`, code
included. That is the intended development behavior.

## 7. Prove it with the test suite

Stop the app (Ctrl-C) or use a second terminal:

```sh
uv run pytest
```

The suite builds its own scratch database (`volunteerdb_test`), so your
seeded data is untouched. Expected: all tests pass in a couple of minutes.

## Where next

- Take the [coordinator's tour](coordinator-tour.md) to see the app as its
  users do.
- Prefer the whole app containerized too?
  `podman compose --profile app up -d --build` serves the same thing from a
  container.
- Development loop: `make dev` restarts on source changes;
  [Run the test suite](../how-to/run-tests.md) covers testing habits.

  :::{note}
  The long form is `VDB_RELOAD=true uv run python -m volunteerdb.main`, and
  the `python -m` part matters. Reload respawns a worker that re-imports the
  module, where only the `__mp_main__` guard in `main.py` calls `ui.run()`.
  The `volunteerdb` console script imports it as `volunteerdb.main` instead,
  so the guard never fires and startup fails with "You must call ui.run() to
  start the server". Without reload, either form is fine.
  :::
