# Site configuration

Everything that differs between one parish's deployment and another's lives in
a single file, `deploy/sites/<name>.toml`. The deploy renders it into systemd
units, the two wrapper scripts, and the application's environment file; nothing
else in the repository holds a site's values.

Choose a site with `SITE` (`VDB_SITE`, under the hood):

```sh
make deploy-dry SITE=<your-site>
```

There is no default. On a repository that can deploy more than one parish,
guessing which one is a way to deploy the wrong one.

:::{note}
This page documents what the **deploy** renders. What the **application**
reads at runtime is [Configuration](configuration.md) — the deploy writes most
of it, from the values below.
:::

## Where each layer lives

`deploy/sites/<name>.toml`
: What differs per parish — the keys on this page. Committed; contains no
  secrets.

`deploy/siteconf.py`
: What is the same on every instance: paths, image and network names, the
  container UID, the Postgres tuning string, the sync exclusion lists. Change
  these when the *stack* changes, never to stand up another parish.

`/etc/volunteerdb/env` on the server
: Generated. Holds the values above plus the secrets, which the deploy
  generates once and reads back on every later run — see
  [Rotate secrets](../how-to/rotate-secrets.md).

## Starting a new site

```sh
cp deploy/sites/example.toml deploy/sites/myparish.toml
```

Fill in every key and commit it. `example.toml` carries a comment on each one
and is checked against the real sites by the test suite, so it cannot fall
behind. The ordered walkthrough — DNS, Caddy, mail, Google Drive, the first
deploy — is [Stand up a new instance](../how-to/new-instance.md).

Missing keys, unknown keys, a value of the wrong TOML type (`caddy = "true"`,
`listen_port = "8090"`), an unparsable `public_ip`, and a `[site] name` that
disagrees with the filename are all rejected when the file loads, before any
operation runs — at `make test`, rather than as a template error halfway
through a deploy.

## `[site]`

`name`
: Must equal the filename stem, so that `VDB_SITE=x` always deploys `x`.

`org_name`
: The organisation, written the way you would say it — `St. Timothy's`,
  `Holy Family Parish`. Appears in outbound mail ("Your VolunteerDB account
  at …"), in the copyright line of the manual served at `/manual`, and its
  name and mail domain become context-specific terms in the
  [password policy](../explanation/auth.md), so a parish's own name cannot be
  used as a password. Becomes `VDB_ORG_NAME`.

`timezone`
: IANA zone, e.g. `America/Toronto`. Date-typed values — election deadlines,
  event reminder windows — mean "through the end of that day *here*", not in
  the container's UTC clock. The host's backup and Drive-sync timers fire on
  this zone too, from this same key, so the app's notion of the parish day
  and the hour its backup runs cannot drift apart. Becomes `VDB_TIMEZONE`.

## `[host]`

`ssh_host`
: How pyinfra reaches the server: an `ssh_config` alias, a hostname, or an
  address. The `DEPLOY_HOST` environment variable overrides it, which is how
  CI targets a host whose name is not committed.

`ssh_user`
: The deploy installs packages and writes under `/etc`, so `root`.

`public_ip`
: The server's public address, where the DNS A record points. With
  `[proxy] caddy = true` the proxy step asserts `domain` resolves to it from
  the server before touching Caddy; nothing else reads it.

`domain`
: The public hostname. Caddy serves TLS for it, and it is the default for
  `VDB_PUBLIC_BASE_URL` — the origin used in emails sent by nightly jobs,
  which have no live request to derive one from.

`listen_port`
: Host loopback port the app publishes on and the reverse proxy sends to.
  Change it only if something else on the machine already uses it. With
  `[proxy] caddy = true` the deploy writes it into the Caddyfile; otherwise it
  must match the `reverse_proxy` (or `proxy_pass`) line in yours.

## `[proxy]`

`caddy`
: `true`: the deploy installs Caddy from its upstream apt repository, owns
  `/etc/caddy/Caddyfile` outright, validates each new version before
  installing it, opens `http`/`https` in firewalld if that is running, and
  reloads Caddy. A hand-written Caddyfile already on the server is copied
  aside once as `Caddyfile.bak-pre-managed-<date>`. Assumes the server sits
  on its own public address: the step checks DNS first and then fetches
  `https://<domain>/login` from the server itself.
  `false`: the deploy never touches Caddy or the firewall; terminate TLS
  yourself from `deploy/examples/`. The reasoning is in
  [Production deployment architecture](../explanation/deployment.md#the-reverse-proxy-managed-or-yours).

`extra`
: Verbatim Caddyfile text appended after the VolunteerDB site block — other
  sites the same Caddy should keep serving. A multi-line literal string
  (`'''…'''`) keeps its tabs. Ignored when `caddy = false`. The test suite
  renders it and refuses unbalanced braces, but only `caddy validate` on the
  server knows whether it is a valid Caddyfile — and the deploy runs that
  before the file goes live.

## `[mail]`

`from_address`
: Sender address. **Must be on a domain your mail provider is authorised to
  send for**, or delivery simply fails — leaving another parish's address
  here is the likeliest reason a new instance sends nothing. The
  `drive-sync@…` account that owns Drive-sync history entries is derived from
  this domain.

`from_name`
: Sender display name.

`admin_email`
: The administrator account the deploy bootstraps. Pass `VDB_ADMIN_PASSWORD`
  on the first deploy or there is no way into the instance.

`alert_email`
: Where a failed backup, a failed Drive sync, or a failed nightly job
  reports. Point it at a mailbox a human reads; empty disables the emails.

`contact_email`
: Shown in the manual's footer ("report it to …"). Baked in at image build
  time, so changing it needs a redeploy.

## `[backup]`

`rclone_remote`
: Name of the rclone remote you provision by hand
  ([Back up and restore](../how-to/backup-restore.md)). The encrypting
  wrapper is this name plus `-crypt`; the deploy asserts both exist and fails
  with a pointer if they do not. `"none"` keeps the nightly dumps on the server
  only: no Drive leg, no assertion, and a reminder on every deploy. Empty is
  rejected, so nobody lands there by omission.

`retain_local_days`
: How long plaintext dumps stay on the server. They are root-only files on
  the same host as the live database, so encrypting them would add nothing.

`retain_remote_days`
: How long encrypted copies stay on Drive. Ignored with `rclone_remote = "none"`.

## `[sheets]`

`folder_id`
: The id of the Google Drive folder new roster sheets are created in — the
  folder's id from its URL, not its name: the app addresses it through the
  Drive API. The OAuth credentials that go with it are secrets and live in
  the remote env file, not here. See
  [Sync team rosters with Google Sheets](../how-to/roster-spreadsheets.md).

## `[schedule]`

Five parish-local times, `HH:MM`. The first drives a systemd timer on the
host; the rest are written into the app's environment for its in-process
scheduler.

`backup_at`, `roster_sync_at`
: The backup must come first, so its dump is a restore point taken
  immediately before the roster sync — the only automated bulk write in the
  system.

`fetch_pages_at`, `proposal_digest_at`, `event_reminders_at`
: The remaining in-app jobs, which come after both so they never contend
  with either.

:::{note}
The ordering is enforced, not merely advised: `tests/test_deploy_config.py`
fails if the five times are not strictly increasing. It used to be a comment
repeated in five places.
:::
