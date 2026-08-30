# Authentication design

The population shapes the login system. The users are a few hundred
parishioners of every age and every level of technical comfort. There is no
help desk, and there is one part-time administrator. Every choice below trades
a little orthodoxy for a lot less support work. None of them gives up a
security property that matters for a database of names, phone numbers and
children's-ministry rosters.

## Passwords are optional

One login form has two paths behind it:

- **Email + password**. The app checks the password with argon2. This path is
  open to anyone who set a password, either when they redeemed their invite or
  later on **/account**.
- **Email alone (OTP)**. Leave the password blank, and the app emails a 6-digit
  code. Type the code to complete the sign-in. A code lives 10 minutes and
  stops after 5 wrong attempts. The app can send a new code after 60 seconds.
  It stores only an argon2 hash of the code.

The OTP path exists because "I forgot my password" is the most common failure
for an occasional user. A volunteer who signs in twice a year can stay
*permanently password-less*. Their security then rests on their mailbox. A
password reset would rest on the same mailbox, so the OTP path gives up
nothing and removes the reset dance. The one real restriction: an OTP-only
account cannot use the JSON API, because the API issues a token against a
password. In practice that affects only the administrator.

## What a password has to be

The rules live in one module, `volunteerdb/passwords.py`. The service layer
enforces them, so the GUI, the JSON API, the seed script and the deploy's
admin bootstrap all get the same answer. The rules come from [NIST SP 800-63B
rev. 4 §3.1.1.2](https://pages.nist.gov/800-63-4/sp800-63b.html), *Password
Verifiers*:

- **At least 15 characters**, because a password here is a single factor: it
  opens the session by itself. The spec's 8-character floor applies only to a
  password used *as part of* multi-factor authentication. A floor of 15 sounds
  harsh until you notice that nobody has to type one at all. The emailed-code
  path is always there. The guidance on every form asks for a phrase of 4 or 5
  words rather than a mangled word.
- **At most 128**. That is well past the "at least 64" the spec asks a
  verifier to permit. The cap only bounds the argon2 work that a single request
  can demand. Concurrency has its own bound. Each pass holds 64 MiB while it
  runs, so at most 6 run at once (`auth._PASSWORD_LIMITER`) and the rest queue.
  Without that bound, a burst of sign-ins could ask for more memory than the
  box has.
- **No composition rules.** The app requires no capital, digit or symbol. The
  spec forbids such rules ("SHALL NOT impose other composition rules"). They
  push people towards `Parish2026!` and away from 4 random words.
- **Everything is a legal character**: printable ASCII, spaces and Unicode,
  counted by code point. The app normalizes a password to NFC before it hashes
  it. A phrase typed on a Mac and on a phone then hashes the same.
- **A blocklist, compared against the whole password.** The list is small on
  purpose. The spec says a large list adds little, because the rate limit
  already slows online guesses. The 15-character floor already excludes nearly
  everything in the usual breach corpora. The list holds the *bases*: a fold
  strips extra characters, repeats and leetspeak, so `P@ssw0rd12345678`,
  `passwordpassword` and `MyPassword-2026!` all land on `password`. Next to
  them sit the context-specific terms the spec names: the service's own names
  and the account's email address.
- **Never expires.** "SHALL NOT require subscribers to change passwords
  periodically." The app forces a change only on evidence of compromise, which
  is what an admin's *re-invite* does.
- **No hints, no security questions.** The spec prohibits both outright, and
  neither exists here.

A rejection says which rule the password hit and what to do instead, because
the spec requires the reason *and* guidance towards a strong choice. The app
does not re-check a current password at sign-in. The rules apply when someone
sets a password, so an account whose password predates them stays open.

The app stores a password as argon2id at RFC 9106's 64 MiB / t=3 / p=4, with a
salt for each password. The stored hash carries the cost factors, so a raise
re-stretches every password on its owner's next sign-in. The one
recommendation the app does not follow is the extra keyed hash (the "pepper").
Its key "SHALL be stored separately from the hashed passwords". On a single VM
with no HSM the key would live in `/etc/volunteerdb/env` beside the database
credentials. That buys the appearance of separation, not separation.

## No self-signup, invites instead

An admin [provisions each account](../how-to/manage-users.md), and a
single-use, time-limited invite link activates it. The admin creates an
account *for* somebody, so it adopts the volunteer record at the same email
address unless the admin picks one explicitly. An unlinked account signs in
and then sees an empty app, which reads as a broken login rather than as a
setup problem. The app refuses the match when it is not unambiguous: a
family-shared address holds 2 volunteers, and a volunteer can hold only one
account. In a parish the population is *known*. An open registration form
would only add spam and identity doubt.

## Resets: two ways, both short-lived

The invite link doubles as the password-reset link. A re-invite sends a fresh
link and invalidates the old password, which keeps the admin's side to a
single button. Because the link *is* a reset credential that sits in a mailbox,
it expires: 7 days by default (`VDB_INVITE_TTL_HOURS`). That is a deliberate
deviation from the 24-hour ceiling that SP 800-63B §4.2.1.2 puts on an emailed
recovery code. Many parishioners read email weekly. And what the link grants
is modest: a fresh account, or a reset on an account whose fallback sign-in is
an emailed code anyway.

The app sets and clears the token and the expiry as a pair. It therefore
refuses an expired link exactly like one that never existed: the same message,
and no hint which.

That double duty is precisely why a team leader's invite button
([who can](permissions.md#accounts-enter-the-model-at-the-edge)) cannot reuse
the re-invite path. A re-invite invalidates a password, and that hammer stays
with admins. A leader's button refuses any account that carries a password or
a recorded login. It can therefore only arm a link on an account nobody has
used, where there is no credential to invalidate. A leader can replace a link
that expired unused; a leader cannot replace a password.

The everyday reset needs no admin at all. Anyone can sign in with an emailed
code and set a password on **/account** (header gear → *Password &
sign-in*). SP 800-63B §4.1.2.1 is explicit that this is not account recovery.
When the subscriber can authenticate with another authenticator, the
replacement of a forgotten password "is considered to be the binding of a new
authenticator". Every account here has that second authenticator. So:

- A session that signed in **with the password** must type it again to change
  it. Without that check, an unattended browser could lock its owner out.
- The app does not ask a session that signed in **with an emailed code** for
  the password. That session has already proved the one thing a reset link
  proves.
- Either way, the app emails the account's own address a *Your VolunteerDB
  password changed* notice. That notice travels through a channel that the
  browser which makes the change does not control. SP 800-63B §4.1.2 requires
  exactly that independence.

Expiry is therefore never a lockout, which is what lets the window be short.

## Changing the address

For an account holder the email address is not contact data. It is the login
identifier, and on the password-less path it *is* the credential: anyone who
reads that mailbox can sign in with a code. A change of that address therefore
binds a new authenticator; it does not merely correct a phone number.

Until now the app had it both ways. The sign-in address could not change at
all. The address on the volunteer record was an ordinary edit. Any leader, or
the volunteer themself, could point it at an address nobody had checked. A
typo cost a volunteer every notice the app sends and the sign-in code with it.
It also handed the account to whoever owned the typo.

So **your own** address moves only after the new one proves itself. You type
it on **/account** or in the edit dialog on your own profile. The app stages
it and mails a single-use link to the address you claimed. Nothing on file
changes until someone opens that link. The account still works at the old
address for the whole window, which is what makes expiry harmless. A second
request replaces the staged address and kills the previous link, so a retype
fixes a typo and nobody has to wait.

The address that the change *replaces* hears about it too, twice, and this is
the part that matters most. SP 800-63B §4.1.2 requires a notice through "a
mechanism independent of the transaction", and here that independence is the
entire defence. A session someone else drives can type a new address, but it cannot
suppress what lands in the mailbox the account currently uses.

The first message goes out **at request time**, while the change is still
open. The old address still signs in, and its owner can cancel the change from
/account before anyone opens the link. The second is the receipt at
confirmation, the moment the address really changes. It looks like the *Your
VolunteerDB password changed* notice, and it says plainly that this is the
last message that mailbox will get. Without the first, a hijacked session
takes an account silently. Without the second, a volunteer who missed the
warning never learns why the app went quiet.

The link lasts **24 hours**, the ceiling SP 800-63B §4.2.1.2 puts on a code
sent to an email address. This time the app takes the number straight, rather
than stretched the way the invite link is. The invite's week-long deviation
buys reachability for someone who would otherwise stay locked out. Here a
missed window loses nothing, so there is nothing to trade the spec's number
against. Hence a constant in `services/users.py`, not a setting: it is not a
parish preference.

To open the link only *offers* the change; a button applies it. Mail scanners
and corporate link-checkers follow URLs on their own. A single-use token that
acts on a GET is a token the recipient's antivirus spends before they ever see
it. That is the same reason the invite page asks for a click. And unlike the
invite link, this one signs nobody in. It grants one address swap and nothing
else, so a leaked link costs the address, not the account.

Confirmation moves **both** addresses together: the login identifier and the
volunteer record behind it. That is what carries the change to every team the
volunteer serves on. A membership holds no address of its own, so rosters,
event notices, substitution calls and the exported spreadsheet all read the
volunteer record live. They pick up the new address on the next send.

The app discards a one-time code still in flight at the same moment. That code
went to the old mailbox to prove control of an identifier that no longer
exists. It must not be spendable against the new one.

Two people can *claim* the same address at once. `pending_email` is
deliberately not unique, because a unique constraint there would let anyone
park an address and lock its real owner out. Only the first to confirm gets
it. The app refuses the second confirmation: it finds the address taken and
clears its own dead link. The app also throttles address-change requests to 5
per account per 15 minutes. It charges that throttle before it learns whether
the address is taken.

**Somebody else's** address stays an ordinary edit. A leader who corrects a
bounced address usually does so *because* the volunteer cannot read their
mail. To wait for the volunteer to confirm would make the correction
impossible. The same goes for the roster spreadsheet import. A staged change
there would leave the sheet and the database in disagreement, and the next
nightly sync would "correct" it back. Those edits touch the volunteer record
only, never the login address, and every one of them is in `volunteer_history`
with the actor who made it.

The residual risk is honest and worth a plain statement: a leader can still
point a teammate's contact address somewhere else. What that does *not* do is
move their sign-in address. And `invite_volunteer` refuses to mint an account
at an address that another account already holds.

The JSON API declines to change your own address rather than half-do it. The
API sends no email by design (see `api/events.py`), so it cannot run the
exchange. A `PATCH /api/volunteers/{id}` that quietly dropped the field would
look like success. `GET /api/auth/me` reports `pending_email` and its expiry,
never the token.

## Anti-enumeration throughout

The login form will not confirm whether an email has an account. An unknown
email burns a dummy argon2 check, so the timing looks identical. The OTP step
gives every address the same answer: *If that address has an account, a
sign-in code is on its way.* Throttles add to that: 5 failures per email and
30 per IP per 15 minutes, and 10 OTP requests per IP per hour. Together they
keep the volunteer directory from a leak through the front door.

That limit is also what SP 800-63B §3.2.2 requires of any password verifier.
So a mistyped *current* password on /account spends from the same per-email
budget: it is one more guess at the same secret. The throttle is in-process, a
sliding window. That is exactly right for a single-process deployment, and it
would be the first thing to revisit if the app ever scaled out.

The app deliberately does not throttle code *entry* on its own. A code dies
after 5 wrong guesses, and the app caps requests for a fresh one at 10 per IP
per hour. The ceiling is therefore roughly 50 guesses an hour against a
6-digit space. That is about 1 in 20,000 odds of a hit in each hour of
sustained attack. A third limiter on the verify step would buy nothing, and it
would give a wrong-code typo the power to lock a volunteer out.

## Sessions: remember-me with an app-side clock

*Keep me signed in* issues a 90-day session; otherwise a session lasts 1 day.
The app stores the expiry that counts **server-side** and checks it on every
action. That includes websocket events, which server-rendered NiceGUI makes
the actual carrier of user activity. The cookie's own `max_age` (92 days) is
just an outer bound. Sign-out clears the session, and a rotation of the
storage secret [signs everyone out at once](../how-to/rotate-secrets.md).
Production adds `Secure` to the cookie, because Caddy terminates TLS.

When a signed-out browser opens `/login`, an invite link or an
address-confirmation link, the app also **mints a fresh session id**. NiceGUI
assigns that id on a browser's first request and never changes it, and the
server keys its per-user storage by it. An id planted on somebody's browser (a
shared kiosk, an XSS on a sibling subdomain) would otherwise become the
*authenticated* id at sign-in. The app rotates the id on the way in rather
than at sign-in itself. Sign-in happens over the websocket, where no response
is left to carry a `Set-Cookie`. The app never rotates a signed-in browser:
that would read as a random logout.

## Every emailed link is hashed like a password

`POST /api/auth/login` returns a personal Bearer token, and the server keeps
only its SHA-256 digest, so a leaked database does not leak a usable token.
The migration that introduced the hash invalidated every earlier token on this
principle. Each login rotates the token, so to revoke one you log in again or
deactivate the account. A forced reset (*re-invite*) revokes it too, because
the app issued the token against the password that the reset invalidates.
That route is what an admin reaches for on an account they think is
compromised. See [Use the JSON API](../how-to/api-recipes.md).

The app holds the **invite link** and the **address-change link** the same
way, for the same reason. Each one signs its holder in, or moves the address
the account signs in with. A read of the database, or of a backup, must
therefore not hand out a live one. The app stores only the digest, which has a
plain consequence: nobody, admin included, can show a link again after it is
sent. To hand one over means to issue a **fresh** link, and the previous one
goes dead. That is the trade every password-reset flow makes, and it is
why the *invite sent* badge offers *Send again* rather than "show me the link".

A one-time code goes further: the app hashes it with argon2, like a password.
6 digits is a small space, so a plain digest would be worth a grind.
