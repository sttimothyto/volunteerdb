# Configure custom fields and workload

Both are admin configuration pages that shape how volunteers are displayed
parish-wide.

## Define a custom field

Custom fields add parish-specific attributes to every volunteer record —
e.g. a *Safeguarding training* date or a *Preferred contact* choice.

1. Open **`/admin/fields`** (header → *Fields*).
2. *New field*: choose a label and a type — `text`, `number`, `select`
   (with a list of options), `date`, `checkbox`, `integer`, `decimal`,
   `timestamp`, `timestamptz`, `time`, `interval` (a duration), or
   `uuid`.
3. Optionally enable **Show in list** to add the field as a column on the
   `/volunteers` table; use *position* to order fields.
4. The field now appears on every volunteer's detail page and edit dialog,
   and as a column in full-parish exports.

Notes:

- The field's `key` (slug) is derived once at creation and is immutable;
  the label can be renamed freely.
- Typed values are entered in standard formats: dates and timestamps in
  ISO 8601 (`2026-08-17 10:30`, with an offset such as `+02:00` or `Z`
  for `timestamptz`), durations as ISO 8601 like `P1DT2H30M`, and
  decimals as digits (`12.50` — exactness is preserved).
- Values are stored in the volunteer's JSONB `custom` column
  ({ref}`schema <custom_field_def>`) — no migration is needed to add or
  retire fields.
- Deactivating a field hides it without deleting stored values.
- Custom-field columns in spreadsheet exports are informational; imports
  ignore them ([format reference](../reference/spreadsheets.md)).

## Configure workload

Workload turns "how loaded is this volunteer?" into a number and a color.
The model is explained in [The workload model](../explanation/workload.md);
this page is the knobs.

1. Open **`/admin/workload`** (header → *Workload*).
2. **Role multipliers** — how much each role weighs (defaults: leader 3,
   second 2, core 1.5, member 1).
3. **Bands** — ascending score thresholds with a label and color each
   (defaults: green ≤ 4, amber ≤ 8, red unbounded). The last band must be
   unbounded; labels must be unique.
4. **Team weights** — per-team workload weight. Teams without a weight
   count 0, so weight only the ministries that represent real load.

Changes apply immediately (no restart): volunteer lists, the graph's
workload coloring, and profile badges all recompute on next load.

## Verify

Give a test team a weight, then check a member of that team on
`/volunteers`: their workload badge should reflect weight × role
multiplier, in the band your thresholds put it in. Remember workload is
visible only to admins and to leaders/seconds of the volunteer's teams.
