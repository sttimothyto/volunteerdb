# Manage user accounts

There is no self-signup: accounts are handed out. Almost all of that happens
on **`/admin/users`** (header → *Accounts*) and is admin-only; the equivalent
API endpoints are under [`/api/users`](../reference/http-api.md). The one
exception is [inviting one of your own team](#let-a-ministry-leader-invite-their-own-people),
which ministry leaders do from their roster.

## Create one account

1. *New account*: enter the email. Team rights come from the linked
   volunteer's memberships, so leaving the picker on *"— match by email —"*
   links the account to the volunteer holding that address. Pick a volunteer
   explicitly when nobody holds the address yet, or when the address is
   shared by a family — an address held by two volunteers is left unlinked
   rather than guessed at.
2. The account is created with an **invite link** (`/invite/<token>`).
   If outbound email is configured, the invite is emailed; the dialog also
   shows the link so you can hand it out in person or by other means. The
   link is single-use and **expires after 7 days**
   (`VDB_INVITE_TTL_HOURS`) — an expired one shows as *invite expired* on the
   account row, and is not a lockout: see [Reset access](#reset-access-forgot-password-lost-invite).
3. The volunteer opens the link and chooses:
   - **set a password** — they can then sign in with email + password (and
     use the JSON API), or
   - **skip the password** — the account stays *OTP-only*: they sign in by
     requesting a 6-digit code to their email each time.

   Passwords must be at least 15 characters, with no other rules: a phrase of
   four or five words is what the form asks for. Well-known passwords and
   anything built out of the site's name or their own email address are
   refused with an explanation. The reasoning is in
   [Authentication design](../explanation/auth.md).

## Create accounts in bulk

*Create accounts for all volunteers with email* provisions one account per
volunteer that has an email address and no account yet (one account per
shared family email). An account that already exists at a volunteer's
address but is linked to nobody is linked to them instead of being skipped —
run this after an import to adopt accounts created before their volunteer
record existed. The result reports how many were created, linked, and
skipped. Invite links are emailed/available per account as above.

(let-a-ministry-leader-invite-their-own-people)=
## Let a ministry leader invite their own people

Leaders do not need `/admin/users` to close the commonest gap — somebody on
their roster who cannot be reached through the app at all. On a team page, the
per-member **no account** badge turns into an **invite to create account**
button for anyone with full-roster rights on that team: its leader, its
second-in-command, or a core member (and admins everywhere). The same control
sits on the volunteer's side panel and profile page. Hovering reveals it;
keyboard focus does too, and on a touch screen the mail icon marks it and a tap
opens it.

Clicking asks for confirmation, naming the address the link will go to, then
creates the account, emails the invite, and shows the link so it can also be
handed over in person. The badge then reads **invite sent** until the link is
redeemed or runs out; clicking it again brings the link back up, with a *Send
again* button.

Once the link expires unused, the control returns as **send a new invite**. That
is safe precisely because nobody has ever used the account: there is no password
to invalidate. The moment an account has a password or a login recorded against
it, the button disappears and the service refuses — a leader can never reset
somebody's credentials, which stays an admin's job under
[Reset access](#reset-access-forgot-password-lost-invite). No control appears on
an as-of snapshot, or for a volunteer with no email address on file.

Two differences from the bulk button worth knowing, both deliberate:

- **It will not adopt an existing account** at the same address. The bulk
  provision above does, because an admin acting parish-wide wants exactly that.
  A leader acting on one person does not: `volunteer.email` is not unique —
  families share an address — so adopting could hand a parent's login to their
  child. It refuses and points here instead.
- **It only ever creates a plain account** linked to that volunteer. It cannot
  make an admin, relink, disable, or promote.

Over the API this is `POST /api/volunteers/{id}/invite`, with the same
permission rule. Note the API mints the link but does not email it.

## Fix a wrong or missing link

The link-icon button on the account row opens *Linked volunteer*: pick a
volunteer, or *"— not linked —"* to detach. One account per volunteer, so
claiming a volunteer another account already holds is refused. Over the
API this is `PATCH /api/users/{id}` with `{"volunteer_id": 12}` (or `null`
to unlink); omitting the field leaves the link alone.

## Reset access ("forgot password", lost invite)

**Usually you do nothing.** Anyone who has forgotten their password signs in
with an emailed code (email address, password field left blank) and sets a new
one under the header gear → *Password & sign-in* (`/account`). No admin, no
link, no waiting. They get an email confirming the change; tell anyone who
receives one out of the blue to report it.

When you do have to step in, the mail-icon button on the account row (*"New
invite link (resets password)"*) invalidates the current password, issues a
fresh invite link, and emails it if possible. The volunteer redeems it as at
creation. Use it when an account is believed compromised — it is what forces
the password to change — or when someone cannot get at the emailed codes.

That link expires after 7 days by default. An expired link is *not* a
lockout: the account can still sign in with an emailed code and set a password
from `/account`. Re-invite again if a fresh link is genuinely needed, or
adjust `VDB_INVITE_TTL_HOURS` for a run of printed hand-outs (and put it back
after).

If the person cannot receive email at all, an OTP-only account will not work
for them: re-invite and hand the link over in person, inside the window, so
they can set a password.

## Change the address on an account

Volunteers do this themselves, and only they can: **header gear → Password &
sign-in**, or the Edit dialog on their own profile. Typing a new address mails
a confirmation link to *that* address; nothing changes until somebody opens the
link and presses the button, and the account keeps working at the old address
in the meantime. The link lasts 24 hours, works once, and asking again replaces
it. When it is confirmed, the sign-in address and the address on every ministry
roster move together.

The **old** address is told twice: once when the change is asked for — naming
the incoming address and pointing at /account to call it off — and once when it
takes effect. If a volunteer reports one of those messages and did not ask for
the change, somebody else is in their account: deactivate it (below), then
re-invite them at an address they control.

There is no admin button for this, on purpose — see
[Authentication design](../explanation/auth.md#changing-the-address). What an
admin (or a ministry leader) *can* change is somebody else's **contact**
address on their profile, which applies at once and does not touch how they
sign in. That is the path for a bounced address the volunteer cannot fix
themselves. If an account is stuck at an address nobody reads, deactivate it
and create a new one at the right address.

## Promote, demote, deactivate

- **Admin toggle** — grants/revokes the global admin flag (see the
  [permission matrix](../reference/permissions.md#permission-matrix)).
- **Active toggle** — an inactive account cannot sign in (GUI or API);
  the volunteer record and memberships are untouched. Use this for
  departures instead of deleting.

## API tokens

A personal API token is issued by `POST /api/auth/login` with email +
password; each login replaces the previous token. OTP-only accounts have no
password and therefore **cannot use the JSON API** — set a password first,
from `/account` or by redeeming an invite. To revoke a token: re-invite the
account, deactivate it, or have its owner remove the password on `/account`
(which drops the token with it).

## Verify

After provisioning, sign in once as the new account (or ask the volunteer
to) and check the dashboard shows the teams you expect under "My teams".
