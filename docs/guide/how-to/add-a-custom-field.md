# Add a custom field

Add a piece of information that the parish records for every volunteer, for example a training date or a preferred contact method.

## Before you start

- You are signed in as an administrator.
- Decide the name of the field and what kind of value it holds. The kinds are listed below.
- The kind of a field cannot change after you save it. To change it, delete the field and make a new one.

## Steps

1. Click *Fields* in the header.
2. Click *New field*.
3. Type the name in *Label*.
4. Pick the kind in *Type*.
5. For a *Choice* field, type the options in *Options (one per line)*, 1 on each line.
6. Switch on *Show as a column on the volunteers list* if the field must appear in the table.
7. Type a number in *Sort position* to order the field among the others. A lower number comes first.
8. Click *Save*.

## The field types in plain words

| Type | What it holds |
|---|---|
| *Text* | Words, of any length. |
| *Number* | A number, with or without decimals. |
| *Integer* | A whole number. |
| *Decimal* | A number with a decimal point, kept exactly as typed, for money for example. |
| *Choice* | 1 of the options you list. |
| *Checkbox* | Yes or no. |
| *Date* | A day. |
| *Time* | A time of day. |
| *Timestamp* | A day and a time. |
| *Timestamp (with zone)* | A day and a time, with the time zone. |
| *Duration* | A length of time, typed in a special form: `P1DT2H30M` is 1 day, 2 hours and 30 minutes. |
| *UUID* | A long code used by other software. |

## What you see

- The field appears on the *Custom fields* page with a badge for its type, and the badge *in list* if it is a column.
- The field appears on every volunteer's page and in the side panel, under the phone number.
- The field appears in the *Edit* dialog of every volunteer, so leaders and seconds can fill it in.
- The field appears as a column on the *Volunteers* page if you switched that on.
- The field appears as an extra column in every `.csv` export, but not in a team's Google Sheet.
- Whoever can see a volunteer's contact details can see their custom fields.

## If something goes wrong

- If the page says *label is required*, then type a name in *Label*.
- If the page says *label must contain letters or digits*, then the name needs at least 1 letter or digit.
- If the page says *a choice field needs at least one option*, then type at least 1 option.
- To hide a field and keep its values, click the pencil icon on its row, switch *Active* off, and click *Save*.
- To remove a field, click the red delete icon on its row, then *Delete*. The values stay in the history but are no longer shown.

## Related pages

- [Edit a member's contact details](edit-a-members-contact-details.md)
- [The screens](../reference/screens.md)
- Technical detail: [Configure custom fields and workload](../../how-to/custom-fields-and-workload.md)
