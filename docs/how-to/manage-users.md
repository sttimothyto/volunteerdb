# Manage user accounts

- There is no self-signup: an admin hands out accounts.
- Almost all of that happens on **`/admin/users`** (header → *Accounts*), and
  only an admin can do it. The equivalent API endpoints are under
  [`/api/users`](../reference/http-api.md).
- The one exception is
  [an invite to one of your own team](#let-a-ministry-leader-invite-their-own-people),
  which ministry leaders do from their roster.

## Create one account

1. Click *New account* and enter the email.
   - Team rights come from the linked volunteer's memberships.
   - Leave the picker on *— match by email —* to link the volunteer who
     holds that address.
   - Pick a volunteer explicitly when nobody holds the address yet, or when
     a family shares the address.
   - An address held by two volunteers stays unlinked; the app does not
     guess.
2. The app creates the account with an **invite link** (`/invite/<token>`)
   and emails the invite.
   - As an admin, you also see the link in the dialog. You can hand it out
     in person or by other means.
   - The app keeps only the link's digest, so this is the one moment anyone
     can read it.
   - Once the dialog closes, nobody can show the link again. To hand one
     over later, send a fresh link, which retires the old one.
   - The link is single-use and **expires after 7 days**
     (`VDB_INVITE_TTL_HOURS`).
   - An expired link shows as *invite expired* on the account row. It is not
     a lockout: see
     [Reset access](#reset-access-forgot-password-lost-invite).
3. The volunteer opens the link and chooses one of:
   - **set a password** — they can then sign in with email + password, and
     use the JSON API; or
   - **skip the password** — the account stays *OTP-only*. They sign in with
     a 6-digit code sent to their email each time.

   Passwords must be at least 15 characters, with no other rules. The form
   asks for a phrase of four or five words. The app refuses well-known
   passwords. It also refuses anything built from the site's name or the
   person's own email address. It explains why. The reasons are in
   [Authentication design](../explanation/auth.md).

## Create accounts in bulk

- *Create accounts for all volunteers with email* provisions one account per
  volunteer that has an email address and no account yet. A shared family
  email gets one account.
- An account can already exist at a volunteer's address but be linked to
  nobody. Then the bulk step links it to that volunteer rather than skip
  them.
- Run this after an import, to adopt accounts created before their volunteer
  record existed.
- The result reports how many accounts it created, linked, and skipped.
- The app emails an invite link per account, and shows it, as above.

(let-a-ministry-leader-invite-their-own-people)=
## Let a ministry leader invite their own people

- Leaders do not need `/admin/users` to close the commonest gap: somebody on
  their roster whom the app cannot reach at all.
- On a team page, the per-member *no account* badge turns into an
  *invite to create account* button for anyone with full-roster rights on
  that team. That is its leader, its second-in-command, or a core member,
  and admins everywhere.
- The same control sits on the volunteer's side panel and profile page.
- A hover reveals it. Keyboard focus does too. On a touch screen the mail
  icon marks it, and a tap opens it.
- A click asks for confirmation and names the address the link will go to.
  Then it creates the account and **emails** the invite there.
- Leaders, seconds and core members do not see the link itself. Whoever
  holds the link signs in as that volunteer. The same person can add anybody
  to their team and then correct their address. So a visible link would make
  every never-used account takeable.
- The mail goes to the address on the volunteer's own record, where only
  they can read it. (An admin still sees the link, for hand-delivery.)
- The badge then reads *invite sent* until somebody redeems the link or it
  runs out. A click on it says an invite is already out and offers
  *Send again*, which mails a fresh link and retires the previous one. Nobody
  can bring the old link back up; the app stores only its digest.
- Once the link expires unused, the control returns as *send a new invite*.
  That is safe because nobody has ever used the account: there is no
  password to invalidate.
- The moment an account has a password or a login recorded against it, the
  button disappears and the service refuses. A leader can never reset
  somebody's credentials. That stays an admin's job under
  [Reset access](#reset-access-forgot-password-lost-invite).
- No control appears on an as-of snapshot, or for a volunteer with no email
  address on file.

Two differences from the bulk button, both deliberate:

- **It does not adopt an account that already exists** at the same address.
  The bulk provision above does, because an admin who acts parish-wide wants
  exactly that. A leader who acts on one person does not. `volunteer.email`
  is not unique (families share an address), so an adoption could hand a
  parent's login to their child. It refuses and points here instead.
- **It only ever creates a plain account** linked to that volunteer. It
  cannot make an admin, relink, disable, or promote.

Over the API:

- This is `POST /api/volunteers/{id}/invite`, with the same permission rule
  and the same split.
- An admin gets `invite_token` back. For anybody else, the app mails the
  link and the response carries only `invite_expires_at`.
- It is the one API route that sends email, because the alternative is a
  credential that reaches nobody.

## Fix a wrong or missing link

- The link-icon button on the account row opens *Linked volunteer*. Pick a
  volunteer, or *— not linked —* to detach.
- One account per volunteer: the app refuses a claim on a volunteer that
  another account already holds.
- Over the API this is `PATCH /api/users/{id}` with `{"volunteer_id": 12}`
  (or `null` to unlink). If you omit the field, the link stays as it is.

## Reset access ("forgot password", lost invite)

- **Usually you do nothing.** Anyone who forgot their password signs in with
  an emailed code (email address, password field left blank). Then they set
  a new password under the header gear → *Password & sign-in* (`/account`).
  No admin, no link, no wait.
- They get an email that confirms the change. Tell anyone who receives one
  out of the blue to report it.
- When you do have to step in, use the mail-icon button on the account row
  (*New invite link (resets password)*). It invalidates the current
  password, issues a fresh invite link, and emails it if possible. The
  volunteer redeems it as at creation.
- Use it when you believe an account is compromised; it is what forces the
  password to change. Also use it when someone cannot get at the emailed
  codes.
- That link expires after 7 days by default. An expired link is *not* a
  lockout: the account can still sign in with an emailed code and set a
  password from `/account`.
- Re-invite again if a fresh link is genuinely needed. Or adjust
  `VDB_INVITE_TTL_HOURS` for a run of printed hand-outs (and put it back
  after).
- If the person cannot receive email at all, an OTP-only account will not
  work for them. Re-invite and hand the link over in person, inside the
  window, so they can set a password.

## Change the address on an account

- Volunteers do this themselves, and only they can: header gear →
  *Password & sign-in*, or the *Edit* dialog on their own profile.
- When they type a new address, the app mails a confirmation link to *that*
  address. Nothing changes until somebody opens the link and presses the
  button. The account continues to work at the old address in the meantime.
- The link lasts 24 hours and works once. A new request replaces it.
- After the confirmation, the sign-in address and the address on every
  ministry roster move together.
- The app tells the **old** address twice: once at the request, and once
  when the change takes effect. The first message names the incoming address
  and points at /account to call it off.
- If a volunteer reports one of those messages and did not ask for the
  change, somebody else is in their account. Deactivate it (below), then
  re-invite them at an address they control.
- There is no admin button for this, on purpose. See
  [Authentication design](../explanation/auth.md#changing-the-address).
- What an admin (or a ministry leader) *can* change is somebody else's
  **contact** address on their profile. That applies at once and does not
  touch how they sign in. It is the path for a bounced address the volunteer
  cannot fix themselves.
- If an account is stuck at an address nobody reads, deactivate it and
  create a new one at the right address.

## Promote, demote, deactivate

- **Admin toggle** — grants or revokes the global admin flag (see the
  [permission matrix](../reference/permissions.md#permission-matrix)).
- **Active toggle** — an inactive account cannot sign in (GUI or API). The
  volunteer record and memberships stay untouched. Use this for departures
  instead of a deletion.

## API tokens

- `POST /api/auth/login` with email + password issues a personal API token.
  Each login replaces the previous token.
- OTP-only accounts have no password and therefore **cannot use the JSON
  API**. Set a password first, from `/account` or with an invite.
- To revoke a token: re-invite the account, deactivate it, or have its owner
  remove the password on `/account` (which drops the token with it).

## Verify

- After the provision, sign in once as the new account (or ask the volunteer
  to).
- Check that the dashboard shows the teams you expect under *My teams*.
