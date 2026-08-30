# Sign in without a password

A parish has a few hundred volunteers of every age, no help desk, and one
part-time administrator. Most volunteers sign in twice a year, and the
password they chose in January is gone by June. So the site is built so that
nobody has to remember a password at all. What it asks instead is something
every volunteer already has: a mailbox.

## Why an emailed code is enough

On the sign-in page you type your email address and leave *Password
(optional)* blank. The site emails you a 6-digit code, and you type it in
and click *Sign in with code*. The code works for 10 minutes and for 5
tries, and *Resend code* gets you a fresh one after a minute. Whoever reads
your mailbox can sign in as you, and that is the point: your mailbox is the
key. A forgotten password would be reset through the same mailbox anyway, so
the code gives up nothing and skips the reset. An usher who serves at Easter
and at Christmas never has to remember anything.

The sign-in page never says whether an address has an account. Whatever you
type, the message is the same: *If that address has an account, a sign-in
code is on its way.* Otherwise anyone could type names into the page and
learn who serves in the parish. Too many wrong tries pause the page for a
few minutes, for the same reason.

Tick *Keep me signed in* and this device stays signed in for 90 days. Leave
it unticked on a shared computer, and the site forgets you after 1 day.

## Why a password is optional

Some people prefer a password: the parish secretary who signs in every day,
or anyone whose email is slow to arrive. The gear menu's *Password &
sign-in* opens the *Your account* page, where you set one. A password must
be at least 15 characters long. A phrase of four or five unrelated words is
the easiest to remember and the hardest to guess. No capital, digit or
symbol is required, and the password never expires. If you signed in with a
code, you can set or change a password without the old one.

*Remove password* takes the password off again, and the account goes back
to emailed codes. Whenever a password is set, changed or removed, the site
emails the account *Your VolunteerDB password changed*. That email lands in
your mailbox, which nobody at your keyboard can stop, so an unwanted change
cannot pass in silence.

## Why the invite link expires

There is no sign-up form, because the parish knows who its volunteers are.
An administrator, or a leader of your team, creates the account for you. The
email *Your VolunteerDB account* holds a link that opens the account once,
after you tick *I agree to keep personal information confidential*. The link
is a key in a mailbox, so it stops working after a set time, usually 7 days.
A link that ran out is no lockout: type your email on the sign-in page,
leave the password blank, and a code arrives. The site cannot show a used
link again; a leader or an administrator sends a fresh one, and the old one
dies.

A fresh link is not harmless for an account in use. An administrator's fresh
link removes the account's password, which is how an account in the wrong
hands is reset. A leader can therefore send a link only to an account nobody
has used yet, and can never undo a password that works.

## What happens to your old address when you change it

Your email address is more than a phone number on a roster. It is how you
sign in, and with a code it is the key itself. So your own address moves
only after the new one proves itself. On *Your account*, you type the new
address and click *Send confirmation*. The new address gets *Confirm your
new VolunteerDB address*, with a link that leads to a button, *Confirm this
address*. Until that button is clicked nothing changes: you still sign in at
the old address, and the link dies after 24 hours.

The old address hears about it twice. At once, it gets *Your VolunteerDB
address is being changed*, while you can still click *Cancel* on *Your
account*. At confirmation, it gets *Your VolunteerDB address changed*, the
last message that mailbox will get. If somebody else were at your keyboard,
they could type a new address, but they cannot stop what lands in your
mailbox. The first message lets you stop the change; the second tells you
why the site went quiet. A typo costs nothing: type the address again, and
the earlier link stops working.

When the change is confirmed, both addresses move together: the one you
sign in with, and the one on every roster you serve on. A leader who
corrects a member's address on a roster does it at once, without
confirmation, because a bounced address cannot confirm anything. That
correction changes the roster only, never the address the member signs in
with. The old address gets *Your VolunteerDB address was changed*, so a
change the member did not ask for is seen and reported.

## Related pages

- [Sign in with an emailed code](../how-to/sign-in-with-a-code.md)
- [Change your password](../how-to/change-your-password.md)
- [Change your email address](../how-to/change-your-email-address.md)
- [Invite a volunteer to create an account](../how-to/invite-a-volunteer.md)
- [Send an invite again](../how-to/resend-an-invite.md)
- [The emails the site sends](../reference/emails.md)
- Technical detail: [Authentication design](../../explanation/auth.md)
