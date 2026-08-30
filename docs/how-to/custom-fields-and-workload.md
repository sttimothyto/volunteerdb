# Configure custom fields and workload

Both are admin configuration pages. They shape how the site shows volunteers
parish-wide.

## Define a custom field

Custom fields add parish-specific attributes to every volunteer record, for
example a *Safeguarding training* date or a *Preferred contact* choice.

1. Open **`/admin/fields`** (header → *Fields*).
2. Click *New field*.
3. Type a label.
4. Choose a type:
   - `text`, `number`, `integer`, `decimal`
   - `select`, with a list of options
   - `date`, `time`, `timestamp`, `timestamptz`, `interval` (a duration)
   - `checkbox`, `uuid`
5. Optionally, turn on *Show as a column on the volunteers list*. The
   field then becomes a column on the `/volunteers` table.
6. Use *Sort position* to order the fields.
7. Click *Save*.

You see the field on every volunteer's detail page and edit dialog, and as a
column in full-parish exports.

Notes:

- The site derives the field's `key` (slug) once, at creation. The key is
  immutable; you can rename the label freely.
- Type a typed value in its standard format:
  - a date or timestamp in ISO 8601 (`2026-08-17 10:30`, with an offset such
    as `+02:00` or `Z` for `timestamptz`)
  - a duration in ISO 8601, like `P1DT2H30M`
  - a decimal as digits (`12.50`; the site keeps it exact)
- The volunteer's JSONB `custom` column holds the values
  ({ref}`schema <custom_field_def>`). No migration is needed to add or retire
  a field.
- When you deactivate a field, the site hides it and keeps the stored values.
- Custom-field columns in spreadsheet exports are informational. Imports
  ignore them ([format reference](../reference/spreadsheets.md)).

## Configure workload

Workload turns "how loaded is this volunteer?" into a number and a color.
[The workload model](../explanation/workload.md) explains the model; this
page is the knobs.

1. Open **`/admin/workload`** (header → *Workload*).
2. Set the *Role multipliers*: how much each role weighs. The defaults are
   leader 3, second 2, core 1.5, member 1.
3. Set the *Colour bands*: score thresholds from low to high, each with a
   label and a color. The defaults are green ≤ 4, amber ≤ 8, red unbounded.
   The last band must be unbounded. Labels must be unique.
4. Click *Save settings*.
5. Set the *Team workload weights*: a workload weight for each team. New
   teams start at 1. Clear a team's weight to put it back to 0, which
   excludes the team from workload scores altogether.
6. Click *Save weights*.

Changes apply at once, with no restart. Volunteer lists, the workload colors
of the graph, and profile badges all recompute on the next load.

## Verify

1. Give a test team a weight.
2. Open `/volunteers` and check a member of that team. Their workload badge
   must show weight × role multiplier, in the band your thresholds put it in.

Remember that workload is visible only to admins and to the leaders and
seconds of the volunteer's teams.
