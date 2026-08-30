# Site configuration

- One file, `deploy/sites/<name>.toml`, holds everything that differs between
  one parish's deployment and another's.
- The deploy renders it into the systemd units, the two wrapper scripts, and
  the application's environment file.
- Nothing else in the repository holds a site's values.

Select a site with `SITE` (`VDB_SITE` underneath):

```sh
make deploy-dry SITE=<your-site>
```

- There is no default.
- A repository can deploy more than one parish. A guessed default could
  deploy the wrong one.

:::{note}
This page documents what the **deploy** renders. The **application** reads
[Configuration](configuration.md) at run time. The deploy writes most of that
file from the values below.
:::

## Where each layer lives

`deploy/sites/<name>.toml`
: What differs per parish: the keys on this page. The file is committed. It
  contains no secrets.

`deploy/siteconf.py`
: What is the same on every instance: paths, image and network names, the
  container UID, the Postgres tuning string, the sync exclusion lists. Change
  these values when the *stack* changes. Do not change them to stand up
  another parish.

`/etc/volunteerdb/env` on the server
: Generated. It holds the values above plus the secrets. The deploy generates
  the secrets once and reads them back on every later run. See
  [Rotate secrets](../how-to/rotate-secrets.md).

## Starting a new site

```sh
cp deploy/sites/example.toml deploy/sites/myparish.toml
```

- Fill in every key and commit the file.
- `example.toml` carries a comment on each key.
- The test suite checks `example.toml` against the real sites, so it cannot
  fall behind.
- The ordered walkthrough (DNS, Caddy, mail, Google Drive, the first deploy)
  is [Stand up a new instance](../how-to/new-instance.md).

The site loader rejects the file when it loads, before any operation runs,
for each of these errors:

- a missing key
- an unknown key
- a value of the wrong TOML type (`caddy = "true"`, `listen_port = "8090"`)
- a `public_ip` that does not parse
- a `[site] name` that differs from the filename

The rejection happens at `make test`, not as a template error halfway through
a deploy.

## `[site]`

`name`
: The value must equal the filename stem, so `VDB_SITE=x` always deploys `x`.

`org_name`
: The organisation, written the way you say it: `St. Timothy's`,
  `Holy Family Parish`. It appears in outbound mail ("Your VolunteerDB account
  at …") and in the copyright line of the manual served at `/manual`. The
  name and the mail domain become context-specific terms in the
  [password policy](../explanation/auth.md), so a parish cannot use its own
  name as a password. The value becomes `VDB_ORG_NAME`.

`timezone`
: An IANA zone, for example `America/Toronto`. A date-typed value (an election
  deadline, an event reminder window) means "through the end of that day
  *here*". It does not mean the end of that day on the container's UTC clock.
  The host's backup timer fires on this zone too, from this same key. So the
  app's parish day and the hour of the backup cannot drift apart. The value
  becomes `VDB_TIMEZONE`.

## `[host]`

`ssh_host`
: How pyinfra reaches the server: an `ssh_config` alias, a hostname, or an
  address. The `DEPLOY_HOST` environment variable overrides it. That override
  is how CI targets a host whose name is not committed.

`ssh_user`
: The deploy installs packages and writes under `/etc`, so the value is
  `root`.

`public_ip`
: The server's public address, where the DNS A record points. With
  `[proxy] caddy = true`, the proxy step asserts from the server that
  `domain` resolves to it, before it touches Caddy. Nothing else reads it.

`domain`
: The public hostname. Caddy serves TLS for it. It is the default for
  `VDB_PUBLIC_BASE_URL`, the origin used in the emails that nightly jobs
  send. Those jobs have no live request to derive an origin from.

`listen_port`
: The host loopback port that the app publishes on and the reverse proxy
  sends to. Change it only if something else on the machine already uses it.
  With `[proxy] caddy = true`, the deploy writes it into the Caddyfile.
  Otherwise it must match the `reverse_proxy` (or `proxy_pass`) line in your
  own proxy configuration.

## `[proxy]`

`caddy`
: `true`: the deploy manages Caddy.
  - The deploy installs Caddy from its upstream apt repository.
  - The deploy owns `/etc/caddy/Caddyfile` outright.
  - The deploy validates each new version of the file before it installs it.
  - If firewalld runs, the deploy opens `http` and `https` in it.
  - The deploy reloads Caddy.
  - The deploy copies a hand-written Caddyfile already on the server aside
    once, as `Caddyfile.bak-pre-managed-<date>`.
  - The step assumes that the server sits on its own public address. It
    checks DNS first, then fetches `https://<domain>/login` from the server
    itself.

  `false`: the deploy never touches Caddy or the firewall.
  - Terminate TLS yourself, from `deploy/examples/`.
  - The reasoning is in
    [Production deployment architecture](../explanation/deployment.md#the-reverse-proxy-managed-or-yours).

`extra`
: Verbatim Caddyfile text, appended after the VolunteerDB site block.
  - Put the other sites that the same Caddy must continue to serve here.
  - A multi-line literal string (`'''…'''`) keeps its tabs.
  - The deploy ignores it when `caddy = false`.
  - The test suite renders it and refuses unbalanced braces.
  - Only `caddy validate` on the server knows whether it is a valid
    Caddyfile. The deploy runs that check before the file goes live.

## `[mail]`

`from_address`
: The sender address. **It must be on a domain that your mail provider is
  authorised to send for.** Otherwise delivery fails. Another parish's
  address left here is the likeliest reason a new instance sends nothing. The
  `drive-sync@…` pseudo-account that owns the roster-sheet sync's history
  entries takes its domain from this address. That name predates the
  sheet-based sync.

`from_name`
: The sender display name.

`admin_email`
: The administrator account that the deploy bootstraps. Pass
  `VDB_ADMIN_PASSWORD` on the first deploy. Otherwise there is no way into
  the instance.

`alert_email`
: Where a failed backup or a failed nightly job (the roster-sheet sync among
  them) reports. Point it at a mailbox that a person reads. An empty value
  disables the emails.

`contact_email`
: Shown in the manual's footer ("report it to …"). The image build bakes it
  in, so a change needs a redeploy.

## `[backup]`

`rclone_remote`
: The name of the rclone remote that you provision by hand
  ([Back up and restore](../how-to/backup-restore.md)).
  - The name of the wrapper remote that encrypts is this name plus `-crypt`.
  - The deploy asserts that both exist, and fails with a pointer if they do
    not.
  - `"none"` keeps the nightly dumps on the server only: no Drive leg, no
    assertion, and a reminder on every deploy.
  - The deploy rejects an empty value, so nobody lands there by omission.

`retain_local_days`
: How long plaintext dumps stay on the server. They are root-only files on
  the same host as the live database, so encryption would add nothing.

`retain_remote_days`
: How long encrypted copies stay on Drive. The backup ignores it with
  `rclone_remote = "none"`.

## `[sheets]`

`folder_id`
: The id of the Google Drive folder where the app creates new roster sheets.
  Use the folder's id from its URL, not its name: the app addresses the folder
  through the Drive API. The OAuth credentials that go with it are secrets.
  They live in the remote env file, not here. See
  [Sync team rosters with Google Sheets](../how-to/roster-spreadsheets.md).

## `[schedule]`

- Five parish-local times, in `HH:MM`.
- The first drives a systemd timer on the host.
- The deploy writes the other four into the app's environment, for its
  in-process scheduler.

`backup_at`, `roster_sync_at`
: The backup must come first. Its dump is then a restore point taken
  immediately before the roster sync, the only automated bulk write in the
  system.

`fetch_pages_at`, `proposal_digest_at`, `event_reminders_at`
: The remaining in-app jobs. They come after both, so they never contend with
  either.

:::{note}
`tests/test_deploy_config.py` enforces the order: it fails unless each of the
5 times is later than the one before. Before, the order was a comment repeated
in 5 places.
:::
