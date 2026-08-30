# Your first development instance

In about 10 minutes you will have VolunteerDB on your machine, with a seeded
demo parish. You will sign in as its administrator and prove the
installation with the test suite.

**You need:**

- Linux with [uv](https://docs.astral.sh/uv/) and podman
  (`sudo apt install podman podman-compose`) installed.
- A clone of this repository as your working directory.

:::{tip}
`make seed` does steps 1–4 in one command, and `make dev` does step 5. This
tutorial spells them out so that you know what each one does. The targets
are one-line wrappers over exactly these commands.
:::

## 1. Create your environment file

```sh
cp .env.example .env
```

- Everything can stay at its default for development.
- `VDB_STORAGE_SECRET` can stay empty. The app then generates a random key
  each boot, which only means your session ends when you restart the app.
- To stay signed in across restarts, set it to a long random string.
  `python3 -c "import secrets; print(secrets.token_hex(32))"` makes one.
- `make seed` writes the file with a generated secret if the file does not
  exist. It leaves a file that exists alone.

## 2. Start PostgreSQL

```sh
podman compose up -d db
```

- This runs PostgreSQL 17 in a container with a persistent volume, published
  on `127.0.0.1:5432`.
- You see: after a few seconds, `podman ps` shows the db container with the
  status *healthy*.

## 3. Create the schema

```sh
uv run alembic upgrade head
```

- The first `uv run` also creates the project's virtualenv.
- You see: a short `INFO` line per migration, which ends at the newest
  revision, and no traceback.

## 4. Seed the demo parish

```sh
uv run python scripts/seed.py
```

This loads a whole parish:

- 34 teams
- about 150 volunteers
- membership history for the timeline features
- 5 custom fields
- some 55 events on both sides of today
- 6 proposals
- 6 public ministry pages
- 33 logins

You see: the script ends with a count of each and the notable accounts:

```
Seeded a demo parish:
     34  teams
    151  volunteers
    293  memberships
    ...
Every login below uses the password: demo
  admin@example.org                demo           administrator
  maria.alvarez@example.org        demo           ministry leader (two teams, red workload)
  felix.garcia@example.org         demo           plain member
  dominic.ferraro@example.org      demo           clergy — sits on every voting roll
```

- The clergy login is the useful one for elections. Those 3 volunteers sit
  on *every* proposal's voting roll, so Fr. Dominic can score a ballot on
  any seat in the parish.
- The script refuses to run on a non-empty database. That is the script at
  work, as designed. `make fresh` wipes the volume first.

## 5. Run the app and sign in

```sh
uv run volunteerdb
```

1. Open <http://localhost:8080>. The app redirects you to the login page.
2. Sign in as `admin@example.org` with the password `demo`.
3. Leave *Keep me signed in* ticked if you like. That is the 90-day session.

`demo` is a seed-only convenience:

- It is far below the 15-character
  [policy](../explanation/auth.md#what-a-password-has-to-be).
- The app will refuse it the moment you try to *set* it from `/account` or
  the API. The seed writes the hash directly.
- Any password you choose yourself must still clear the policy.

## 6. Look around

You land on the *Dashboard*. 5 things confirm that the seed did its job:

- Header → *Elections*: *Vacancies* lists *Hospitality* and a dozen other
  seats, the teams seeded without a leader or a second.
- Header → *Elections*: the proposals show one proposal in every state,
  from *Nominating* through *Awaiting decision* to *Appointed*.
- Header → *Events*: rosters on both sides of today. Past Masses have their
  attendance already derived; the future ones have RSVPs and 2 open calls
  for a substitute.
- Search for `maria` in the quick-search box and open *Maria Alvarez*. She
  leads 2 ministries, her workload badge is deep in the red, and her
  timeline chart shows an ended Youth Group spell.
- Header → *Teams* → *Liturgy*: a team with sub-teams and a roster you can
  manage. Try the *View as of (YYYY-MM-DD)* date picker (header settings
  gear) with last year's date, and note the amber read-only banner.

Signed out, <http://localhost:8080/ministries/> lists the 6 ministries that
publish a public home page.

`.env` has no `VDB_SMTP2GO_API_KEY`, so the app prints every email it
"sends" to the terminal where `uv run volunteerdb` runs, code included. To
see one, sign out and log in as Maria with a blank password: the OTP path.
That is the intended development behavior.

## 7. Prove it with the test suite

Stop the app (Ctrl-C), or use a second terminal:

```sh
uv run pytest
```

- The suite builds its own scratch database (`volunteerdb_test`), so your
  seeded data is untouched.
- You see: all tests pass in a couple of minutes.

## Where next

- Take the [coordinator's tour](coordinator-tour.md) to see the app as its
  users do.
- Do you prefer the whole app in a container too?
  `podman compose --profile app up -d --build` serves the same thing from a
  container.
- Development loop: `make dev` restarts on source changes.
  [Run the test suite](../how-to/run-tests.md) covers test habits.

  :::{note}
  The long form is `VDB_RELOAD=true uv run python -m volunteerdb.main`, and
  the `python -m` part matters. Reload respawns a worker that re-imports the
  module. Only the `__mp_main__` guard in `main.py` calls `ui.run()`. The
  `volunteerdb` console script imports the module as `volunteerdb.main`
  instead. So the guard never fires, and startup fails with "You must call
  ui.run() to start the server". Without reload, either form is fine.
  :::
