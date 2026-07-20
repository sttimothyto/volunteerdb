# Your first development instance

In about ten minutes you will have VolunteerDB running locally with a
seeded demo parish, sign in as its administrator, and prove the
installation with the test suite.

**You need:** Linux with [uv](https://docs.astral.sh/uv/) and podman
(`sudo apt install podman podman-compose`) installed, and a clone of this
repository as your working directory.

## 1. Create your environment file

```sh
cp .env.example .env
```

Open `.env` and set `VDB_STORAGE_SECRET` to any long random string
(`python3 -c "import secrets; print(secrets.token_hex(32))"` makes one).
Everything else can stay at its default for development.

:::{note}
This step is not optional. Podman-compose does not apply `${VAR:-default}`
fallbacks, so without a `.env` the database container would initialize with
a literal `${POSTGRES_PASSWORD:-volunteerdb}` as its password. If that
already happened, reset with `podman compose down -v && podman compose up -d db`.
:::

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
a short `INFO` line per migration, ending at revision `0004`, and no
traceback.

## 4. Seed the demo parish

```sh
uv run python scripts/seed.py
```

This loads 16 teams, 30 volunteers, membership history for the timeline
features, two custom fields, and three logins. It ends by printing:

```
  admin login:  admin@sttimothy.example / changeme
  leader login: maria.alvarez@example.org / volunteer
  member login: felix.garcia@example.org / volunteer
```

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

- **Holes to fill** lists *Hospitality* — the demo team seeded without a
  leader.
- Search for `maria` in the quick-search box and open **Maria Alvarez**:
  she leads two ministries, her capacity badge is deep in the red, and her
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
- Development loop: `VDB_RELOAD=true uv run volunteerdb` restarts on source
  changes; [Run the test suite](../how-to/run-tests.md) covers testing
  habits.
