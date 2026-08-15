# Authentication design

The login system is shaped by its population: a few hundred parishioners of
every age and technical comfort level, no help desk, and one part-time
administrator. Every choice below trades a little orthodoxy for a lot less
support burden — without giving up the security properties that matter for
a database of names, phone numbers, and children's-ministry rosters.

## Passwords are optional

One login form, two paths behind it:

- **Email + password** — verified with argon2. Available to anyone who set a
  password, whether when redeeming their invite or later from **/account**.
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

## What a password has to be

The rules live in one module, `volunteerdb/passwords.py`, and are enforced in
the service layer — so the GUI, the JSON API, the seed script and the deploy's
admin bootstrap all get the same answer. They come from [NIST SP 800-63B
rev. 4 §3.1.1.2](https://pages.nist.gov/800-63-4/sp800-63b.html), *Password
Verifiers*:

- **At least 15 characters**, because a password here is single-factor: it
  opens the session by itself. (The spec's eight-character floor applies only
  to passwords used *as part of* multi-factor authentication.) Fifteen sounds
  harsh until you notice nobody has to type one at all — the emailed-code path
  is always there, and the guidance on every form asks for a phrase of four or
  five words rather than a mangled word.
- **At most 128**, well past the "at least 64" the spec asks verifiers to
  permit; the cap only bounds the argon2 work a single request can demand.
- **No composition rules.** No required capital, digit or symbol — the spec
  forbids them ("SHALL NOT impose other composition rules"), and they push
  people towards `Parish2026!` and away from four random words.
- **Everything is a legal character**: printing ASCII, spaces, Unicode,
  counted by code point. Passwords are NFC-normalized before hashing so a
  phrase typed on a Mac and on a phone hash the same.
- **A blocklist, compared against the whole password.** Deliberately a small
  one: the spec says large lists add little because online guessing is already
  rate-limited, and the 15-character floor already excludes nearly everything
  in the usual breach corpora. What is listed are the *bases* — folding undoes
  padding, doubling and leetspeak, so `P@ssw0rd12345678`, `passwordpassword`
  and `MyPassword-2026!` all land on `password`. Alongside them sit
  context-specific terms the spec names: the service's own names and the
  account's email address.
- **Never expires.** "SHALL NOT require subscribers to change passwords
  periodically." A change is forced only on evidence of compromise, which is
  what an admin's *re-invite* does.
- **No hints, no security questions.** Both are prohibited outright, and
  neither exists here.

Rejections say which rule was hit and what to do instead, because the spec
requires the reason *and* guidance towards a strong choice. Existing passwords
are not re-checked at sign-in: the rules apply when a password is set, and
nobody is locked out of an account whose password predates them.

Storage is argon2id at RFC 9106's 64 MiB / t=3 / p=4, salted per password, with
the cost factors written into the stored hash — raise them and every password
re-stretches on its owner's next sign-in. The one recommendation not followed
is the extra keyed hash ("pepper"): its key "SHALL be stored separately from
the hashed passwords", and on a single VM with no HSM it would live in
`/etc/volunteerdb/env` beside the database credentials, which buys the appearance
of separation rather than separation.

## No self-signup, invites instead

Accounts are [provisioned by an admin](../how-to/manage-users.md) and
activated through single-use, time-limited invite links. An account is created *for*
somebody, so it adopts the volunteer record at the same email address
unless the admin picks one explicitly — an unlinked account signs in
successfully and then sees an empty app, which reads as a broken login
rather than as missing configuration. The match is refused when it is not
unambiguous: a family-shared address holds two volunteers, and a volunteer
may hold only one account. In a parish the population is
*known* — an open registration form would only add spam handling and
identity doubt.

## Resets: two ways, both short-lived

The invite link doubles as the password-reset link (re-invite = fresh link, old
password invalidated), which keeps the admin's side to a single button. Because
it *is* a reset credential sitting in a mailbox, it expires: 24 hours by
default (`VDB_INVITE_TTL_HOURS`), the ceiling SP 800-63B §4.2.1.2 puts on a
recovery code sent to an email address. Token and expiry are set and cleared as
a pair, so a link that has run out is refused exactly like one that never
existed — same message, no hint which.

The everyday reset needs no admin at all. Anyone can sign in with an emailed
code and set a password from **/account** (header gear → *Password &
sign-in*). §4.1.2.1 is explicit that this is not account recovery:
"Replacement of a forgotten password where the subscriber can authenticate with
one or more other authenticators is considered to be the binding of a new
authenticator." Every account here has that second authenticator. So:

- a session that signed in **with the password** must re-type it to change it —
  otherwise an unattended browser could lock its owner out;
- a session that signed in **with an emailed code** is not asked for it, having
  already proved the one thing a reset link proves;
- either way the account's own address is emailed a "your password changed"
  notice, through a channel the browser making the change does not control
  (§4.1.2 requires exactly that independence).

Expiry is therefore never a lockout, which is what lets the window be short.

## Anti-enumeration throughout

The login form will not confirm whether an email has an account: unknown
emails burn a dummy argon2 check so timing looks identical, and the OTP
step responds "code sent" either way. Combined with throttling — 5 failures
per email and 30 per IP per 15 minutes, 10 OTP requests per IP per hour —
this keeps the volunteer directory from leaking through the front door. That
limit is also what SP 800-63B §3.2.2 requires of any password verifier, so a
mistyped *current* password on /account spends from the same per-email budget:
it is one more guess at the same secret. Throttling is in-process (a sliding
window), which is exactly right for a single-process deployment and would be
the first thing to revisit if the app ever scaled out.

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
