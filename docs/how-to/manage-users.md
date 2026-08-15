# Manage user accounts

Accounts are admin-provisioned — there is no self-signup. All of this
happens on **`/admin/users`** (header → *Accounts*); the equivalent API
endpoints are under [`/api/users`](../reference/http-api.md).

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
   link is single-use and **expires after 24 hours**
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

That link expires after 24 hours by default. An expired link is *not* a
lockout: the account can still sign in with an emailed code and set a password
from `/account`. Re-invite again if a fresh link is genuinely needed, or raise
`VDB_INVITE_TTL_HOURS` for a run of printed hand-outs (and lower it after).

If the person cannot receive email at all, an OTP-only account will not work
for them: re-invite and hand the link over in person, inside the window, so
they can set a password.

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
