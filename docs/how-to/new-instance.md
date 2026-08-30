# Stand up a new instance

For a second parish — a production deployment of your own, from scratch. It
assumes you have already run a development instance
([Your first development instance](../tutorials/install-and-run.md)), so you
know what the application does before you put it in front of anyone.

Allow a couple of hours, most of it waiting on other people's consoles: DNS
propagation, a mail provider verifying your domain, a Google Cloud consent
screen. The deploy itself takes minutes.

The steps are ordered. Three of them have to happen in the order given —
noted where they do.

## 0. What you need first

- **A server.** Debian 13 on its own public address (any VPS), root SSH
  access, at least 4 GB RAM, 2 vCPU and 20 GB of disk, ports 80 and 443
  reachable. The deploy uses `apt` and installs podman and — unless you keep
  your own reverse proxy (step 3) — Caddy; nothing needs to be on the machine
  beforehand.
- **A domain** you control, and the ability to add an A record.
- **A Google account** to own the roster spreadsheets and the backups. A
  parish account, not a person's — people leave.
- **A mail provider.** These instructions use SMTP2GO, which is what the
  application speaks; a free tier is enough for a parish.
- **`uv`** on your own machine, and a clone of this repository.

## 1. Write the site file

```sh
cp deploy/sites/example.toml deploy/sites/myparish.toml
```

Fill in every key — each carries a comment, and the
[site configuration reference](../reference/site-config.md) explains what each
one reaches. Then commit it. Nothing in it is secret; that is why it is
committed.

From here on, `SITE=myparish` selects it: `make deploy SITE=myparish`.

## 2. DNS

Add an A record for `[host] domain` pointing at `[host] public_ip`. Do this
before the first deploy: Caddy cannot obtain a certificate for a name that
does not yet resolve to it, and with `[proxy] caddy = true` the deploy checks
the record from the server and refuses to touch Caddy until it is right.

```sh
dig +short vdb.myparish.org      # should print your server's address
```

## 3. Reverse proxy

Decide who owns it, in `[proxy]` of the site file.

`caddy = true` (what `example.toml` ships with)
: The deploy installs Caddy from its upstream apt repository, writes
  `/etc/caddy/Caddyfile`, validates it, opens `http`/`https` in firewalld if
  that is running, and reloads Caddy. There is nothing to do in this step.
  Other sites the same Caddy should serve go into `[proxy] extra`, verbatim.
  A hand-written Caddyfile already on the server is copied aside once, as
  `Caddyfile.bak-pre-managed-<date>`, before the deploy takes the file over.

`caddy = false`
: The deploy never touches Caddy or the firewall. Install your own reverse
  proxy and terminate TLS from `deploy/examples/Caddyfile` or
  `deploy/examples/nginx.conf`, substituting your domain and
  `[host] listen_port`. Choose this when Caddy on the server already serves
  sites you would rather keep by hand, when another proxy is already there, or
  when the server is not on its own public address (behind NAT, or fronted by
  a CDN): the managed mode checks DNS and then fetches
  `https://<domain>/login` from the server itself, and neither works there.

Either way, TLS needs nothing further — Caddy obtains and renews a Let's
Encrypt certificate on its own once DNS resolves — and the block must carry
`encode zstd gzip`, because the app serves nothing compressed itself.

## 4. Outbound mail

Sign up with SMTP2GO, add the domain of `[mail] from_address` as a sending
domain, and publish the SPF, DKIM and DMARC records it gives you. Then create
an API key.

:::{warning}
`from_address` must be on a domain your provider is authorised to send for.
An instance left pointing at another parish's address will simply fail to
send — invitations, sign-in codes and every alert — and the only sign is a
`mail.send_failed` line in the log.
:::

You can defer this: with no API key the application prints every message to
its log instead of sending it, which is enough to get in and look around. It
is not enough to invite anyone.

## 5. Google Cloud project

Both the backups and the roster-sheet decoration reuse one OAuth client, so
this is set up once.

1. Create a Google Cloud project owned by the parish account.
2. Enable the **Google Drive API**, the **Google Sheets API** and the
   **Google Calendar API**. (rclone uses Drive; the roster-sheet sync uses
   Sheets; the parish calendar uses Calendar. A missing one surfaces later as
   a `SERVICE_DISABLED` error with an activation link.)
3. Create an **OAuth client ID** of type *Desktop*. Keep the client ID and
   secret for step 7.
4. Set the OAuth consent screen to **In production** (or *Internal*, on a
   Workspace domain). Left in *Testing*, refresh tokens expire after seven
   days and backups start failing.

## 6. First deploy

```sh
VDB_ADMIN_PASSWORD='…' VDB_SMTP2GO_API_KEY='api-…' make deploy SITE=myparish
```

:::{warning}
`VDB_ADMIN_PASSWORD` on the **first** deploy is the only way into a new
instance. Without it the admin bootstrap is skipped and there is no account to
sign in with. It must clear the
[password policy](../explanation/auth.md#what-a-password-has-to-be); a
rejected one fails with a readable reason before the database is touched.

The account it creates is `[mail] admin_email`.
:::

**Expect this run to fail at the end**, on:

```
ERROR: rclone remote … not configured on this host.
```

That is the intended order. The application — and, with `caddy = true`, TLS —
is fully deployed by that point; the failure is the backup step refusing to
install a timer for a destination that does not exist yet. Step 7 provisions
it. (A site file with `[backup] rclone_remote = "none"` is green here, and
keeps its nightly dumps on the server only.)

Preview any deploy with `make deploy-dry SITE=myparish`; it connects and
reports what would change without changing anything.

## 7. Backups

Follow [one-time Drive setup](backup-restore.md#one-time-drive-setup): it
provisions the rclone Drive remote from the OAuth client of step 5, and then
the `crypt` wrapper that encrypts the Drive leg.

:::{warning}
Record the crypt password **off the server**, immediately — in a password
manager *and* on paper with the parish records. Total server loss is exactly
the scenario Drive backups exist for, and it is also the scenario in which the
only on-server copy of that password is gone.
:::

Without a Google account at all, set `[backup] rclone_remote = "none"`: the
nightly dump is still taken and pruned on the server, nothing leaves it, and
every deploy reminds you. A lost disk then loses the database and its backups
together, so treat it as a stopgap.

## 8. Deploy again

```sh
make deploy SITE=myparish
```

Green this time, through the backup step.

You now have a running instance. Sign in at `https://<your domain>/login` as
`[mail] admin_email` and start entering teams and volunteers, or import a
[roster spreadsheet](roster-spreadsheets.md).

## 9. Roster sheets on Drive (optional)

Only if you want the nightly two-way sync with Google Sheets. Read
[Sync team rosters with Google Sheets](roster-spreadsheets.md) first.

Create the Drive folder new roster sheets go in and put its id in
`[sheets] folder_id`, make the roster template sheet (shared read-only to
anyone with the link), then run `scripts/google_authorize.py` as the parish
Google account and pass the results on one deploy:

```sh
VDB_TEMPLATE_SHEET_URL='https://docs.google.com/…' \
  VDB_SHEETS_CLIENT_ID='…' VDB_SHEETS_CLIENT_SECRET='…' \
  VDB_SHEETS_REFRESH_TOKEN='…' \
  make deploy SITE=myparish
```

Like the mail key, they are read back from the server on later runs, so you
pass them once. The same token drives the
[parish Google Calendar](google-calendar-sync.md), which needs no setting of
its own: the sync creates the calendar on its first run.

## 10. Deploy from CI (optional)

Pushing to `main` can deploy, gated on lint, the test suite and the docs
build. Create a `production` GitHub Environment with:

`DEPLOY_SSH_KEY`
: A private key whose public half is in the server's
  `/root/.ssh/authorized_keys`. Generate a fresh one for this
  (`ssh-keygen -t ed25519 -f deploy_key -N ''`); do not reuse your own.

`DEPLOY_KNOWN_HOSTS`
: The server's host key, from `ssh-keyscan <host>`. Pins the identity so a
  deploy cannot be redirected.

`VDB_SMTP2GO_API_KEY`
: Optional. Unset expands to empty, which is falsy, so the deploy keeps the
  key already on the host; setting it rotates the key on every run.

`DEPLOY_HOST`
: Optional. Overrides `[host] ssh_host`, which is useful when that is an
  `ssh_config` alias that means nothing on a runner. Whatever you put here has
  to match what `DEPLOY_KNOWN_HOSTS` pins.

Then name the site in a **repository** variable `VDB_SITE` (Settings →
Secrets and variables → Actions → Variables) — not an environment variable:
the deploy job's `if` runs before the environment is chosen, so it can only
see repository variables. Until it is set, a push to `main` runs lint, the
tests and the docs build and skips the deploy, which is also what a fork of
this repository gets by default. Nothing in the workflow file is edited; the
link GitHub shows beside each deployment is read from the site file. Adding a
required reviewer to the environment turns the deploy into a manual approval,
again without touching the workflow.

## Verify

- `https://<your domain>/login` serves the sign-in page over HTTPS, and you
  can sign in as the admin account.
- On the server, both containers are up and healthy:
  ```sh
  systemctl status volunteerdb-app volunteerdb-db
  systemctl list-timers 'volunteerdb-*'
  ```
- A backup runs on demand and reaches Drive:
  ```sh
  systemctl start volunteerdb-backup.service
  journalctl -u volunteerdb-backup -n 20
  ```
- Invite yourself a second account from **Accounts** and confirm the email
  arrives. If the API key is unset it will be in the log instead:
  `journalctl -u volunteerdb-app | grep MAIL`.

## Afterwards

- [Deploy and upgrade](deploy.md) — the routine path, and rolling back.
- [Manage users](manage-users.md) — inviting leaders and volunteers.
- [Rotate secrets](rotate-secrets.md).
- [Production deployment architecture](../explanation/deployment.md) — why the
  stack is shaped this way, and its known gaps.
