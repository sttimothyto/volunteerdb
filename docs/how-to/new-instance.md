# Stand up a new instance

This page is for a second parish: a production deployment of your own, from
scratch.

- Run a development instance first
  ([Your first development instance](../tutorials/install-and-run.md)). Then
  you know what the application does before you put it in front of anyone.
- Plan for a couple of hours. Most of that time you wait on other people's
  consoles: DNS propagation, a mail provider that verifies your domain, a
  Google Cloud consent screen.
- The deploy itself takes minutes.
- The steps are in order. 3 of them must occur in the order given; the page
  says so at each one.

## 0. What you need first

- **A server.** Debian 13 on its own public address (any VPS), with root SSH
  access.
  - At least 4 GB RAM, 2 vCPU and 20 GB of disk. Ports 80 and 443 must be
    reachable.
  - The deploy uses `apt` and installs podman. It also installs Caddy, unless
    you keep your own reverse proxy (step 3).
  - You do not need to install anything on the machine before the deploy.
- **A domain** that you control, and the ability to add an A record.
- **A Google account** to own the roster spreadsheets and the backups. Use a
  parish account, not a person's account. People leave.
- **A mail provider.** These instructions use SMTP2GO, which is the provider
  the application speaks to. A free tier is enough for a parish.
- **`uv`** on your own machine, and a clone of this repository.

## 1. Write the site file

```sh
cp deploy/sites/example.toml deploy/sites/myparish.toml
```

1. Fill in every key. Each key carries a comment, and the
   [site configuration reference](../reference/site-config.md) explains what
   each key reaches.
2. Commit the file. Nothing in it is secret. That is why you commit it.

From here on, `SITE=myparish` selects it: `make deploy SITE=myparish`.

## 2. DNS

- Add an A record for `[host] domain` that points at `[host] public_ip`.
- Do this before the first deploy. Caddy cannot get a certificate for a name
  that does not yet resolve to it.
- With `[proxy] caddy = true`, the deploy checks the record from the server.
  It refuses to touch Caddy until the record is right.

```sh
dig +short vdb.myparish.org      # should print your server's address
```

## 3. Reverse proxy

Decide who owns it, in `[proxy]` of the site file.

`caddy = true` (what `example.toml` ships with)
: The deploy installs Caddy from its upstream apt repository. It writes
  `/etc/caddy/Caddyfile` and validates it. It opens `http`/`https` in
  firewalld if firewalld runs, and reloads Caddy. There is nothing to do in
  this step. Put other sites that the same Caddy must serve into
  `[proxy] extra`, verbatim. The deploy copies a hand-written Caddyfile aside
  once, as `Caddyfile.bak-pre-managed-<date>`, before it takes the file over.

`caddy = false`
: The deploy never touches Caddy or the firewall. Install your own reverse
  proxy and terminate TLS from `deploy/examples/Caddyfile` or
  `deploy/examples/nginx.conf`. Substitute your domain and
  `[host] listen_port`. Choose this mode when Caddy on the server already
  serves sites that you keep by hand, or when another proxy is already there.
  Also choose it when the server is not on its own public address (behind
  NAT, or fronted by a CDN). The managed mode checks DNS and then fetches
  `https://<domain>/login` from the server itself, and neither works there.

- In both modes, TLS needs nothing more. Caddy gets and renews a Let's
  Encrypt certificate on its own once DNS resolves.
- The block must carry `encode zstd gzip`, because the app serves nothing
  compressed itself.

## 4. Outbound mail

1. Sign up with SMTP2GO.
2. Add the domain of `[mail] from_address` as a sender domain.
3. Publish the SPF, DKIM and DMARC records that SMTP2GO gives you.
4. Create an API key.

:::{warning}
`from_address` must be on a domain that your provider is authorised to send
for. An instance left pointed at another parish's address will fail to send:
invitations, sign-in codes and every alert. The only sign is a
`mail.send_failed` line in the log.
:::

- You can defer this step. With no API key, the application prints every
  message to its log and does not send it.
- That is enough to get in and look around. It is not enough to invite
  anyone.

## 5. Google Cloud project

Both the backups and the roster-sheet decoration reuse one OAuth client, so
you set this up once.

1. Create a Google Cloud project owned by the parish account.
2. Enable the **Google Drive API**, the **Google Sheets API** and the
   **Google Calendar API**. rclone uses Drive. The roster-sheet sync uses
   Sheets. The parish calendar uses Calendar. An API left off surfaces later
   as a `SERVICE_DISABLED` error with an activation link.
3. Create an **OAuth client ID** of type *Desktop*.
4. Keep the client ID and the secret for step 7.
5. Set the OAuth consent screen to **In production** (or *Internal*, on a
   Workspace domain). If it stays in *Testing*, refresh tokens expire after
   7 days and backups start to fail.

## 6. First deploy

```sh
VDB_ADMIN_PASSWORD='…' VDB_SMTP2GO_API_KEY='api-…' make deploy SITE=myparish
```

:::{warning}
`VDB_ADMIN_PASSWORD` on the **first** deploy is the only way into a new
instance. Without it, the deploy skips the admin bootstrap and there is no
account to sign in with. The password must clear the
[password policy](../explanation/auth.md#what-a-password-has-to-be). The
deploy rejects a bad one with a readable reason before it touches the
database.

The account it creates is `[mail] admin_email`.
:::

**Expect this run to fail at the end**, on:

```
ERROR: rclone remote … not configured on this host.
```

- That is the intended order.
- The application is fully deployed by that point. With `caddy = true`, TLS
  is too.
- The failure is the backup step. It refuses to install a timer for a
  destination that does not exist yet.
- Step 7 provisions that destination.
- A site file with `[backup] rclone_remote = "none"` is green here. It keeps
  its nightly dumps on the server only.
- Preview any deploy with `make deploy-dry SITE=myparish`. It connects and
  reports what would change, and changes nothing.

## 7. Backups

- Follow [one-time Drive setup](backup-restore.md#one-time-drive-setup).
- That page provisions the rclone Drive remote from the OAuth client of
  step 5.
- It then provisions the `crypt` wrapper that encrypts the Drive leg.

:::{warning}
Record the crypt password **off the server**, immediately. Put it in a
password manager *and* on paper with the parish records. Drive backups exist
for total server loss. That is also the case in which the only on-server copy
of that password is gone.
:::

- Without a Google account at all, set `[backup] rclone_remote = "none"`.
- The server still takes and prunes the nightly dump. Nothing leaves the
  server, and every deploy reminds you.
- A lost disk then loses the database and its backups together. Treat this
  mode as a stopgap.

## 8. Deploy again

```sh
make deploy SITE=myparish
```

- This run is green, through the backup step.
- The instance now runs. Sign in at `https://<your domain>/login` as
  `[mail] admin_email`.
- Start to enter teams and volunteers, or import a
  [roster spreadsheet](roster-spreadsheets.md).

## 9. Roster sheets on Drive (optional)

- Do this step only if you want the nightly two-way sync with Google Sheets.
- Read [Sync team rosters with Google Sheets](roster-spreadsheets.md) first.

1. Create the Drive folder that new roster sheets go in.
2. Put its id in `[sheets] folder_id`.
3. Make the roster template sheet.
4. Share the template read-only to anyone with the link.
5. Run `scripts/google_authorize.py` as the parish Google account.
6. Pass the results on one deploy:

```sh
VDB_TEMPLATE_SHEET_URL='https://docs.google.com/…' \
  VDB_SHEETS_CLIENT_ID='…' VDB_SHEETS_CLIENT_SECRET='…' \
  VDB_SHEETS_REFRESH_TOKEN='…' \
  make deploy SITE=myparish
```

- Like the mail key, the deploy reads these values back from the server on
  later runs. You pass them once.
- The same token drives the [parish Google Calendar](google-calendar-sync.md).
  The calendar needs no setting of its own: the sync creates the calendar on
  its first run.

## 10. Deploy from CI (optional)

- A push to `main` can deploy, gated on lint, the test suite and the docs
  build.
- Create a `production` GitHub Environment with these secrets:

`DEPLOY_SSH_KEY`
: A private key whose public half is in the server's
  `/root/.ssh/authorized_keys`. Generate a fresh one for this
  (`ssh-keygen -t ed25519 -f deploy_key -N ''`). Do not reuse your own.

`DEPLOY_KNOWN_HOSTS`
: The server's host key, from `ssh-keyscan <host>`. It pins the identity, so
  nobody can redirect a deploy.

`VDB_SMTP2GO_API_KEY`
: Optional. Unset expands to empty, which is falsy, so the deploy keeps the
  key already on the host. If you set it, the deploy rotates the key on every
  run.

`DEPLOY_HOST`
: Optional. It overrides `[host] ssh_host`. That is useful when the value is
  an `ssh_config` alias that means nothing on a runner. What you put here
  must match what `DEPLOY_KNOWN_HOSTS` pins.

- Then name the site in a **repository** variable `VDB_SITE` (Settings →
  Secrets and variables → Actions → Variables).
- Do not use an environment variable. The deploy job's `if` runs before
  GitHub picks the environment, so it can only see repository variables.
- Until `VDB_SITE` is set, a push to `main` runs lint, the tests and the docs
  build, and skips the deploy. A fork of this repository gets the same by
  default.
- You edit nothing in the workflow file. The link that GitHub shows beside
  each deployment comes from the site file.
- Add a required reviewer to the environment to turn the deploy into a manual
  approval. This too needs no change to the workflow.

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
- Invite yourself a second account from *Accounts* and confirm that the
  email arrives. If the API key is unset, the email is in the log instead:
  `journalctl -u volunteerdb-app | grep MAIL`.

## Afterwards

- [Deploy and upgrade](deploy.md) — the routine path, and how to roll back.
- [Manage users](manage-users.md) — how to invite leaders and volunteers.
- [Rotate secrets](rotate-secrets.md).
- [Production deployment architecture](../explanation/deployment.md) — why the
  stack is shaped this way, and its known gaps.
