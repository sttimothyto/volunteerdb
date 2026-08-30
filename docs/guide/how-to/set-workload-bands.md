# Set the workload bands

Decide how the site scores a volunteer's workload, and which colour each score gets.

## Before you start

- You are signed in as an administrator.
- A volunteer's score adds up, over every team they serve on, the team's weight multiplied by the number for their role.
- The standard numbers for the roles: *Ministry leader* 3, *Second-in-command* 2, *Core team member* 1.5, *Member* 1.
- The standard bands: *green* up to 4, *amber* up to 8, *red* for everything above.
- A team with weight 0 does not count towards anyone's score.
- Only administrators and the leaders and seconds of a volunteer's teams see the score. A volunteer never sees their own.

## Steps

1. Click *Workload* in the header.
2. Under *Role multipliers*, type the number for each role.
3. Under *Colour bands*, type the name of each band in *Label*.
4. Pick the colour of each band in *Colour*.
5. Type the highest score of each band in *up to score*. The last band takes everything above.
6. Click *Save settings*.
7. Under *Team workload weights*, type a weight for each team. Clear the box of a team that must not count.
8. Click *Save weights*.

## What you see

- The page shows *Workload settings saved*, and after the weights *Updated N team weights*.
- Beside each colour the page shows *badge text N:1*: how well the band's name reads on that colour. Below 4.5:1 the page refuses the colour.
- The number of bands is fixed on this page. You can rename and recolour them, and move the limits.
- The new scores and colours appear at once in every place a score is shown:
  - the *Workload* column and the *Workload* filter on the *Volunteers* page;
  - the badge at the top of a volunteer's page and side panel;
  - the coloured dots and the legend of the graph on the *Dashboard*;
  - the *Workload:* row under *Needs attention* on the *Dashboard*;
  - the badge beside each candidate on an election page.
- A team's weight is also on its *Edit team* dialog.

## If something goes wrong

- If the page says *no text reads on …*, then pick a darker or a lighter colour.
- If the page says *band thresholds must be positive and ascending*, then make each *up to score* larger than the one above it.
- If the page says *band labels must be unique*, then give each band a different name.
- If the page says *multipliers must not be negative*, then type 0 or more for each role.

## Related pages

- [Read workload](read-workload.md)
- [The screens](../reference/screens.md)
- Technical detail: [Configure custom fields and workload](../../how-to/custom-fields-and-workload.md)
- Technical detail: [The workload model](../../explanation/workload.md)
