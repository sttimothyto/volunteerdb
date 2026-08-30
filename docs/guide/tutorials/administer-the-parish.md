# Administer the parish

In this tutorial you visit the pages *Accounts*, *Fields* and *Workload* as an administrator. You create an account, add a custom field, change a team weight, upload the parish logo, and read the email banner. It takes about 30 minutes.

## What you need

- You are signed in as an administrator. Your header shows *Accounts*, *Fields* and *Workload*.
- The email address of a volunteer who has no account yet.
- The parish logo, saved as a picture on the device you use.

## 1. Read the Accounts page

1. Click *Accounts* in the header.
2. Find the two buttons at the top: *Create accounts for all volunteers with email* and *New account*.
3. Read one row. It shows an email address and, under it, the name of the linked volunteer.
4. Move the mouse over the 4 icons at the right end of the row.

You see a page with one row per account. A row can carry a badge: *disabled*, *invite pending*, *invite expired* or *email-code sign-in*. The tooltips on the 4 icons are *Change linked volunteer*, *Make admin* or *Revoke admin*, *Disable* or *Enable*, and *New invite link (resets password)*. Do not click the last one now: it cancels the password of that person.

## 2. Create an account

1. Click *New account*.
2. Type the email address of the volunteer in *Email (login)*.
3. Leave *Linked volunteer* at *— match by email —*. The account links to the volunteer with that address.
4. Leave *Parish admin (full access)* off.
5. Click *Create*.
6. Read the dialog *Invite link for*, followed by the address. The link is on the screen, with a button *Copy*.
7. Click *Close*.
8. Click *Accounts* in the header to load the list again.

You see the new row with the badge *invite pending*. The same link was emailed to the volunteer. It works once, and for 7 days. If the email did not arrive, the dialog was the one moment to copy the link and hand it over by other means.

- A row with *email-code sign-in* is an account with no password. The person signs in with an emailed code.
- To close an account, click the *Disable* icon on its row. The volunteer record and the memberships stay.

## 3. Add a custom field

1. Click *Fields* in the header.
2. Click *New field*.
3. Type a label in *Label*, for example *Safeguarding training*.
4. Click *Type* and choose *Date*.
5. Turn on *Show as a column on the volunteers list* if you want the field in the table.
6. Leave *Sort position* at 0.
7. Click *Save*.

You see the page *Custom fields* with the new field in the list. It has a badge *Date*, and the badge *in list* if you turned the switch on. From now on the field is on every volunteer's page and in the *Edit* dialog. Whoever can see the contact details of a volunteer can see the value. The types are *Text*, *Number*, *Choice*, *Date*, *Checkbox*, *Integer*, *Decimal*, *Timestamp*, *Timestamp (with zone)*, *Time*, *Duration* and *UUID*.

- A field of the type *Choice* also asks for *Options (one per line)*.
- The type cannot change after *Save*. The label can.

## 4. Read the workload settings

1. Click *Workload* in the header.
2. Read the text at the top. A score is the sum, over the teams of a volunteer, of the team weight times the role multiplier.
3. Find *Role multipliers*: one box for each of the 4 roles.
4. Find *Colour bands*: a *Label*, a *Colour* and *up to score* for each band. The last band says *everything above*.
5. Find *Team workload weights*: one box for each team.
6. Type 2 in the box of a team whose work is heavier than most.
7. Click *Save weights*.

You see the message *Updated 1 team weight*. The badges on the *Volunteers* page recompute at once for the members of that team. To undo, type 1 in the same box and click *Save weights* again. *Save settings* saves the multipliers and the bands.

- The starting multipliers are 3 for *Ministry leader*, 2 for *Second-in-command*, 1.5 for *Core team member* and 1 for *Member*.
- The starting bands are *green* up to 4, *amber* up to 8, and *red* for everything above.
- Only administrators, and the leaders and seconds of a volunteer's teams, see workload badges. The *Dashboard* never shows your own.

## 5. Upload the parish logo

1. Move the mouse over the logo at the left end of the header. A tooltip says *Change the site logo*.
2. Click the logo.
3. Read the dialog *Site logo*. The preview sits on the colour of the header.
4. Click the box *Drop a logo here (stored as PNG, at most 1000×1000)* and pick the picture.
5. Look at the preview. The picture is scaled to fit, never cropped.
6. Click *Upload*.

You see the message *Logo saved*, and every page now shows the logo in the header. The sign-in page and the public ministry pages show it too. *Remove logo* in the same dialog goes back to the plain placeholder.

## 6. Read the email banner

The site can send 200 emails a day and 1,000 a month. Only administrators see the banner, and only when the site is close to a limit.

1. Open any page and look under the page title.
2. Read the strip, if there is one. An amber strip says *Email sending is heading over its limit*. A red strip says *Email sending is over its limit*.
3. Read the counts: how many emails went out today, and this month.
4. Contact the person the strip names: the administrator who set up this website.

You see no strip on a normal day. Past a limit, messages stop, and that includes the sign-in codes. Nothing on the site can raise the limit, so the fix is a bigger plan, or fewer emails.

## What you learned

- *Accounts* lists every account. *New account* creates one and shows you the invitation link.
- *Fields* adds a property to every volunteer. The type is fixed; the label is not.
- *Workload* holds the multipliers, the colour bands and the team weights behind the workload badges.
- The logo in the header opens the dialog *Site logo*.
- The amber or red strip under the page title warns you that the site is close to its email limit.

## Next steps

- [Manage accounts](../how-to/manage-accounts.md)
- [Read the audit log](../how-to/read-the-audit-log.md)
- [See the parish as it was on a past date](../how-to/see-the-parish-as-of-a-date.md)

## Related pages

- [Add a custom field](../how-to/add-a-custom-field.md)
- [Set the workload bands](../how-to/set-workload-bands.md)
- [Upload the parish logo](../how-to/upload-the-parish-logo.md)
- [Read the workload of your people](../how-to/read-workload.md)
- Technical detail: [Manage user accounts](../../how-to/manage-users.md)
- Technical detail: [Configure custom fields and workload](../../how-to/custom-fields-and-workload.md)
- Technical detail: [Set the site logo](../../how-to/site-logo.md)
