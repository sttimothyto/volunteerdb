# Authentication design

The login system is shaped by its population: a few hundred parishioners of
every age and technical comfort level, no help desk, and one part-time
administrator. Every choice below trades a little orthodoxy for a lot less
support burden — without giving up the security properties that matter for
a database of names, phone numbers, and children's-ministry rosters.

## Passwords are optional

One login form, two paths behind it:

- **Email + password** — verified with argon2. Available to anyone who set
  a password when redeeming their invite.
- **Email alone (OTP)** — leave the password blank and a 6-digit code is
  emailed; typing it in completes sign-in. Codes live 10 minutes, allow 5
  attempts, and can be re-sent after 60 seconds; only an argon2 hash of the
  code is stored.

The OTP path exists because "I forgot my password" is the dominant failure
mode for occasional users. A volunteer who signs in twice a year can stay
*permanently password-less*; their security rests on their mailbox — which
is exactly where a password reset would rest anyway, so the OTP path gives
up nothing while removing the reset dance. The one real restriction:
OTP-only accounts cannot use the JSON API (tokens are issued against a
password), which in practice affects only the administrator.

## No self-signup, invites instead

Accounts are [provisioned by an admin](../how-to/manage-users.md) and
activated through single-use invite links. In a parish the population is
*known* — an open registration form would only add spam handling and
identity doubt. The invite link doubles as the password-reset mechanism
(re-invite = fresh link, old password invalidated), so there is exactly one
"get me in" flow to support.

## Anti-enumeration throughout

The login form will not confirm whether an email has an account: unknown
emails burn a dummy argon2 check so timing looks identical, and the OTP
step responds "code sent" either way. Combined with throttling — 5 failures
per email and 30 per IP per 15 minutes, 10 OTP requests per IP per hour —
this keeps the volunteer directory from leaking through the front door.
Throttling is in-process (a sliding window), which is exactly right for a
single-process deployment and would be the first thing to revisit if the
app ever scaled out.

Code *entry* is deliberately not throttled on its own. A code dies after five
wrong guesses, and requesting a fresh one is capped at 10 per IP per hour, so
the ceiling is roughly 50 guesses an hour against a six-digit space — about
one in twenty thousand odds of a hit in a year of sustained attack. Adding a
third limiter to the verify step would buy nothing and would give a wrong-code
typo the power to lock a volunteer out.

## Sessions: remember-me with an app-side clock

"Keep me signed in" issues a 90-day session, otherwise 1 day. The expiry
that counts is stored **server-side** and checked on every action —
including websocket events, which server-rendered NiceGUI makes the actual
carrier of user activity; the cookie's own `max_age` (92 days) is just an
outer bound. Sign-out clears the session; rotating the storage secret
[signs everyone out at once](../how-to/rotate-secrets.md). Production adds
`Secure` to the cookie since Caddy terminates TLS.

## API tokens are hashed like passwords

`POST /api/auth/login` returns a personal Bearer token whose SHA-256 digest
is all the server keeps — a leaked database does not leak usable tokens
(migration `0004` invalidated all pre-hashing tokens on this principle).
Each login rotates the token, so revocation is "log in again" or
deactivate the account. See [Use the JSON API](../how-to/api-recipes.md).
