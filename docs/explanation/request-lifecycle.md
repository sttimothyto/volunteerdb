# From a click to the database

This page follows one action from the browser to PostgreSQL and back. The
action is a role change on a team roster. A leader clicks the role of a
member, picks a new role, and clicks *Save*. Each step names the function
that does the work, so you can read the code beside the page.

Read [Architecture](architecture.md) first. It states the rules, and this
page shows them at work on one path.

## The path at a glance

```text
browser (Vue)           server (one Python process)                         PostgreSQL
click the role   ─ws─▶  table.on("role") → _role_dialog()          (no database)
click Save       ─ws─▶  busy(save) → run_command(command)
                        page_ctx() → db.transaction()  ───────────▶ BEGIN; set_config('app.user_id')
                          get_actor() → load_actor()   ───────────▶ SELECT app_user, membership, team
                          command(ctx) → memberships.set_role()
                            get_managed()              ───────────▶ SELECT membership
                            require(can_manage_team)                 (pure)
                            membership.role = role
                            session.flush()            ───────────▶ UPDATE membership
                                                                      trigger: INSERT membership_history
                          split_outcome() → policy.plan()            (pure)
                        the block ends                 ───────────▶ COMMIT
                        effects.run()                                (mail, audit, throttle)
                        on_ok → close the dialog
                        refresh() → reread()           ───────────▶ a new transaction: read the roster
```

The path crosses four layers. `ui/` turns the click into a call.
`api/deps.py` holds the context that both front doors share. `services/`
decides, and the database keeps the row and its history.

## Before the click

### One Env for the process

`main.run` calls `env.build()` once. The result is a frozen `Env`. It holds
the settings, the clock, the random source, the mailer, the HTTP clients and
the database engine. `main.create_app` stores it on `app.state.env`.

A `@ui.page` function has no dependency injection, so it reads the `Env`
back with `env.current()`. Services never see the `Env`. They get values,
such as `now` and `tz`, and a session.

### The page load

A request for `/teams/{team_id}` passes `AuthMiddleware` first. The
middleware sends a browser with no session to the sign-in page. Then
`ui/teams_page.py: team_detail` opens `ui/context.py: page_ctx()`. That
context manager does three things:

- It opens `db.transaction()`. The transaction starts with
  `set_config('app.user_id', …, true)`. The value is local to the
  transaction, and the history trigger reads it.
- It loads the reader as an `Actor` with `actors.load_actor()`. The actor
  holds the team ids that the reader can manage, see in full, or see by name.
  A role on a team counts on its sub-teams too.
- It gives the page a `PageCtx`. The context carries the session, the actor,
  the `Env`, one clock reading, the base URL and the notify mode.

Inside the block, the page makes one read:
`services/readmodels.py: team_room()`. The read model returns the roster rows
and the facts about the reader, such as `can_manage`. Then the block ends and
commits. The page draws its widgets only after that point, and
`tests/test_ui_layer.py` fails on a `ui.*` call inside a session block.

The roster section also gets a loader, `_room_loader()`. The section keeps
it to read the roster again after a change.

## The click

The role cell of the roster table is a small Vue template, `_ROLE_CELL`. Its
button runs `$parent.$emit('role', props.row)`. That is the only logic in
the browser. NiceGUI sends the event over its websocket, and the handler runs
on the server.

`table.on("role", …)` calls `_role_dialog()` with the membership id, the
name and the current role from the row. The dialog comes from `ui/forms.py`.
`dialog_card()` draws the card, and `actions()` draws *Cancel* and *Save*.
`actions()` wraps *Save* in `ui/widgets.py: busy()`, which shows a spinner
and blocks a second click. Nothing has touched the database yet.

:::{note}
The membership id comes from the browser, and a browser can send any id. So
the widget does not decide what the reader can do. The service checks the
rights of the reader on every call.
:::

## The command

Every change in the GUI goes through `ui/context.py: run_command()`. The
*Save* handler gives it a small function, the command:

```python
async def command(ctx: PageCtx):
    return await membership_service.set_role(
        ctx.session, ctx.actor, membership_id, TeamRole(role.value)
    )
```

The command turns the value of the widget into a domain type and makes one
service call. It holds no rule of its own. `tests/test_edge_layer.py` fails
on an ORM query in a page, so the read and the write stay in the service.

`run_command()` opens a new `page_ctx()`. The action gets a new transaction
and an actor loaded again from the database. So a leader who lost the role
after the page loaded gets a refusal. The session expiry is checked here too,
because a websocket event never passes `AuthMiddleware`.

## The service

`services/memberships.py: set_role()` is the one function that changes a
role, for both front doors:

```python
managed = await get_managed(session, actor, membership_id)
if isinstance(managed, Err):
    return managed
membership = managed.value
membership.role = role
await session.flush()
return Ok(membership)
```

`get_managed()` reads the membership. If there is no row, it returns
`Err(NotFound)`. Then it checks the rights with
`require(actor.can_manage_team(team_id), …)`. `require()` returns
`Err(Forbidden)` or `None`, so the check is a value in the code.
`tests/test_authorization_layer.py` fails on a check whose result nobody
uses.

A service never raises. It returns `Ok(value)` or `Err(error)`, from
`fp.py`. The error is one of a closed set in `errors.py`. The edge decides
what a refusal means for its reader: a toast in the GUI, a status in the API.

## The flush

`session.flush()` sends `UPDATE membership SET role = … WHERE id = …`. Three
mechanisms react, and no service calls any of them:

- The `versioning()` trigger fires BEFORE UPDATE. It copies the old row into
  `membership_history` and closes its `sys_period`. It stamps `changed_by`
  from `app.user_id`, and `op` as `U`.
- The `after_flush` listener in `audit.py` writes a `db.update` log line
  with the old role and the new role.
- The `after_flush` listener in `team_cache.py` drops the memo of the team
  tree if a `Team` row changed. A role change touches no team row, so the
  memo stays.

`db.py` imports `audit` and `team_cache` for their listeners. The listeners
are on the `Session` class, so they cover every session in the process. For
the trigger and its twin tables, see [History and time travel](history.md).

## Commit or roll back

The `Result` of the command comes back to `run_command()`:

- An `Err` rolls the transaction back with `await ctx.session.rollback()`.
  Then `toast()` shows the refusal. After the rollback, the end of the block
  commits nothing, and `tests/test_uow.py` holds that.
- An `Ok` goes to `split_outcome()`. That function separates the value from
  the domain events, if the service returned an `Outcome`. `set_role()`
  returns a plain `Membership`, so there are no events.
- `policy.plan()` turns events into effects. It is pure, and here it returns
  no effect.
- Then the block ends and PostgreSQL commits. The `after_commit` listener in
  `audit.py` writes a `db.commit` line with the identity of the reader.

A constraint violation at commit raises `IntegrityError`. That is the one
exception left in the path. `run_command()` catches it and shows a
`Conflict` toast.

## After the commit

`effects.run()` does every planned effect. It sends the mail, writes the
audit lines and charges the throttles. It runs after the commit, so mail
never goes out inside a transaction. It never raises, because the work has
already committed. A role change plans no effect, so this step does nothing
here.

Next, `on_ok` closes the dialog. The command gave `refresh=`, so
`run_command()` redraws the roster in place and does not reload the page.
The `refresh()` of the roster calls `reread()`, which opens one more
`page_ctx()` and calls `team_room()` again. The same table gets the new rows,
so the search text, the sort and the table page stay. If the rights of the
reader changed, `refresh()` reloads the whole page instead.

Last, the reader sees *Role updated*. The session factory sets
`expire_on_commit=False` in `db.py: make_sessions`. So the objects that a
service returned stay readable after the commit.

## The same change through the JSON API

`PATCH /api/memberships/{id}` reaches the same service.
`api/memberships.py: update` takes this path:

- `api/deps.py: api_ctx` checks the Bearer token. It opens the transaction,
  sets `app.user_id` and loads the actor, as `page_ctx()` does.
- The route calls `get_managed()` and then `set_role()`. These are the same
  two functions that the GUI calls.
- `raise_http()` turns an `Err` into its HTTP status through `status_of()`.
  For example, `Forbidden` is 403 and `NotFound` is 404. The exception
  unwinds the transaction.
- `api_ctx` commits when the route returns.

A service that returns an `Outcome` goes through `dispatch()` instead of
`raise_http()`. `dispatch()` plans the effects and gives them to the
`BackgroundTasks` of FastAPI. They run after the response, which is after
the commit. `api/events.py: remove_assignment` is a route of this kind.

A request for a substitute shows the effects at work.
`services/events.py: request_sub` returns a `SubRequested` event, and
`policy.py` turns it into mail. The `Ctx` of the API carries
`NotifyMode.digest`, so the API sends no roster mail. The nightly digest
tells the people instead. See [HTTP API](../reference/http-api.md).

## Past dates

A page opened with `?as_of=` gives an instant to the same read functions.
`history.entity()` then puts a union in place of the live table. The union
holds the live rows and the history rows whose `sys_period` contains the
instant. `history.fetch()` runs that query in a separate session. So a past
row never enters the identity map of the live session. On a past date,
`TeamRoom.can_manage` is false, and the roster is read-only.

## The tests that hold the path

| Rule | Test |
|---|---|
| The core has no `raise`, no clock read, no `settings()` and no `uuid4` | `tests/test_purity_layer.py` |
| Every permission check has a result that the code uses | `tests/test_authorization_layer.py` |
| No page or route queries the ORM, or reads the process `Env` when its context holds one | `tests/test_edge_layer.py` |
| No mail, audit line or throttle charge under `api/`, `ui/` or `services/` | `tests/test_effects_layer.py` |
| No `ui.*` call inside a session block, and no refreshable at module level | `tests/test_ui_layer.py` |
| An `Err` rolls back with no exception | `tests/test_uow.py` |
| Each policy rule, checked with values alone | `tests/test_policy.py` |

To run them, see [Run the test suite](../how-to/run-tests.md).

## Read the code in this order

1. `ui/teams_page.py: _role_dialog` has the handler and the command.
2. `ui/context.py: run_command` and `page_ctx` are the unit of work of the GUI.
3. `db.py: transaction` opens the transaction and sets `app.user_id`.
4. `services/memberships.py: set_role` and `get_managed` hold the rule and the check.
5. `effects.py: run` is the one interpreter of effects.
6. `api/memberships.py: update` is the second front door, into the same service.

For the rights that `can_manage_team()` reads, see
[The permission model](permissions.md).
