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

- **A server.** Debian 13, root SSH access, at least 4 GB RAM, 2 vCPU and
  20 GB of disk. The deploy uses `apt` and installs podman; nothing else needs
  to be on the machine beforehand except Caddy (step 3).
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

From here on, `VDB_SITE=myparish` selects it.

## 2. DNS

Add an A record for `[host] domain` pointing at `[host] public_ip`. Do this
before step 3: Caddy cannot obtain a certificate for a name that does not yet
resolve to it.

```sh
dig +short vdb.myparish.org      # should print your server's address
```

## 3. Caddy

Install Caddy on the server, then copy the site block from
`deploy/examples/Caddyfile` into `/etc/caddy/Caddyfile`, substituting your
domain and `[host] listen_port`.

```sh
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
```

The deploy does **not** install, template or reload Caddy. Caddy on your
server may serve other sites, and it owns a certificate lifecycle that a
redeploy has no business restarting. TLS needs nothing further: Caddy obtains
and renews a Let's Encrypt certificate on its own once DNS resolves.

There is nothing to browse to yet — the application is not deployed. Caddy
will answer with a 502 until step 6, which is the expected result at this
point.

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
2. Enable the **Google Drive API** and the **Google Sheets API**. (rclone uses
   Drive; the nightly sheet decoration uses Sheets. Missing the second one
   surfaces later as a `SERVICE_DISABLED` error with an activation link.)
3. Create an **OAuth client ID** of type *Desktop*. Keep the client ID and
   secret for step 7.
4. Set the OAuth consent screen to **In production** (or *Internal*, on a
   Workspace domain). Left in *Testing*, refresh tokens expire after seven
   days and backups start failing.

## 6. First deploy

```sh
VDB_SITE=myparish VDB_ADMIN_PASSWORD='…' VDB_SMTP2GO_API_KEY='api-…' \
  uvx pyinfra==3.10.0 deploy/inventory.py deploy/deploy.py -y
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

That is the intended order. The application is fully deployed by that point —
the failure is the backup step refusing to install a timer for a destination
that does not exist yet. Step 7 provisions it.

Preview any deploy with `--dry` instead of `-y`; it connects and reports what
would change without changing anything.

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

## 8. Deploy again

```sh
VDB_SITE=myparish uvx pyinfra==3.10.0 deploy/inventory.py deploy/deploy.py -y
```

Green this time, through the backup and Drive-sync steps.

You now have a running instance. Sign in at `https://<your domain>/login` as
`[mail] admin_email` and start entering teams and volunteers, or import a
[roster spreadsheet](import-export.md).

## 9. Roster sheets on Drive (optional)

Only if you want the nightly two-way sync with Google Sheets. Read
[Sync team rosters with Google Sheets](drive-roster-sync.md) first — in
particular its one-time verification that file IDs survive re-upload on your
host, which must pass **before** the sync goes live. If IDs are not stable,
every link you have shared with a leader breaks on the first sync.

Then create the `[drive_sync] sheets_folder` folder, make the roster template
sheet, and set its URL on one deploy:

```sh
VDB_SITE=myparish VDB_TEMPLATE_SHEET_URL='https://docs.google.com/…' \
  uvx pyinfra==3.10.0 deploy/inventory.py deploy/deploy.py -y
```

Like the mail key, it is read back from the server on later runs, so you pass
it once.

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

Then set `VDB_SITE` in the workflow's deploy job to your site's name. Adding a
required reviewer to the environment turns the deploy into a manual approval
without touching the workflow.

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
