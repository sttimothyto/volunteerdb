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
   shows the link so you can hand it out in person or by other means.
3. The volunteer opens the link and chooses:
   - **set a password** — they can then sign in with email + password (and
     use the JSON API), or
   - **skip the password** — the account stays *OTP-only*: they sign in by
     requesting a 6-digit code to their email each time.

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

The mail-icon button on the account row (*"New invite link (resets
password)"*): it invalidates the current password, issues a fresh invite
link, and emails it if possible. The volunteer redeems it as at creation.
This is also the password-reset mechanism — there is no separate reset flow.

If the person just cannot receive email, an OTP-only account will not work;
re-invite them and hand over the link directly so they can set a password.

## Promote, demote, deactivate

- **Admin toggle** — grants/revokes the global admin flag (see the
  [permission matrix](../reference/permissions.md#permission-matrix)).
- **Active toggle** — an inactive account cannot sign in (GUI or API);
  the volunteer record and memberships are untouched. Use this for
  departures instead of deleting.

## API tokens

A personal API token is issued by `POST /api/auth/login` with email +
password; each login replaces the previous token. OTP-only accounts have no
password and therefore **cannot use the JSON API** — set a password (via
invite) first. To revoke a token, re-invite the account (invalidates the
password) or deactivate it.

## Verify

After provisioning, sign in once as the new account (or ask the volunteer
to) and check the dashboard shows the teams you expect under "My teams".
