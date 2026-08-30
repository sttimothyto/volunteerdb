# Search query filters

- The search boxes on the dashboard, the volunteers list, the teams list,
  and the events list accept SQL `WHERE`-clause filters alongside plain
  text.
- Text that does not read as a complete boolean condition (a name, a
  half-typed clause, a sentence) runs as the usual substring search.
- A well-formed filter with a problem (an unknown field, a bad value)
  reports the problem. It does not quietly search for the text.
- The app parses the filter and compiles it into the same
  permission-checked queries the pages already run. It is a search syntax,
  not database access.

```text
phone LIKE '555%' AND team = 'Liturgy'
years_served > 2 AND role IN ('leader', 'second')
NOT (notes IS NULL)
created > '2025-01-01'
```

## Syntax

- Combine conditions with `AND`, `OR`, `NOT`, and parentheses.
- Compare with `=`, `!=`, `<`, `<=`, `>`, `>=`, `IN (…)`, and
  `BETWEEN … AND …`.
- Test for an absent value with `IS [NOT] NULL`.
- Match text patterns with `LIKE` and `ILIKE`: `%` matches any run, `_`
  matches one character. `ILIKE` ignores case.
- One side of a comparison is a field, the other a value: quoted text, a
  number, or `true`/`false`.
- The filter does not support functions, subqueries, or field-to-field
  comparisons.
- Write dates and timestamps as quoted ISO 8601: `'2026-08-17'`,
  `'2026-08-17 10:30'`. Where the field carries a zone, add an offset like
  `'+02:00'` or `'Z'`.
- Write durations as quoted ISO 8601, like `'P1DT2H30M'`.
- Write decimals and integers as bare numbers.

## Volunteer fields (dashboard and volunteers list)

| Field | Type | Notes |
|---|---|---|
| `name`, `first_name`, `last_name` | text | `name` is "First Last" |
| `email` | text | private — see below |
| `id` | integer | |
| `created` | timestamp with zone | when the record was created — there is no `joined` field (membership dates are not tracked) |
| `is_active` | true/false | inactive volunteers stay hidden from non-admins regardless |
| `phone`, `notes` | text | private — see below |
| `team` | text | the team's name; membership condition |
| `role` | text | `leader` · `second` · `core` · `member` |
| `custom.<key>` | the field's own type | any custom field by its key (the bare key works too unless it collides with a built-in); private |

## Team fields (teams list)

- Text fields: `name`, `path`, `description`.
- `is_active` (true/false).
- Coverage counts (integers): `leaders`, `seconds`, `core`, `members`,
  `total`, `gaps`.
- A count is readable only where the page shows it. For a team you do not
  manage, a count never matches.
- Query matches keep their ancestor rows, exactly like the substring
  filter, so the tree indent stays intact.

## Event fields (events list)

- Text fields: `title`, `team`, `location`, `you`.
- `date` (date), quoted ISO like `date >= '2026-09-01'`.
- Integers: `filled`, `capacity`.
- `capacity` is absent (`IS NULL`) as soon as one slot is unlimited.
- `you` holds `serving`, `available`, or `unavailable`: your own
  relationship to the event.
- The list is already scoped to the teams you can see, so the filter never
  reveals more than the unfiltered page.

## What each role's filter can see

- Filters obey the same visibility rules as everything else. A filter
  cannot reveal a value the pages would redact.
- **Private fields** are `email`, `phone`, `notes`, and custom values. A
  condition on a private field is *false* for every volunteer whose private
  fields you cannot view.
  - You can view the private fields of yourself, plus of everyone on a team
    where you are leader, second, or core. That is the same scope as the
    plain search.
  - That holds under `NOT` too: `NOT (phone LIKE '555%')` matches, at most,
    the people whose phones you can read.
  - `email` is private like the rest: a condition on it counts only for a
    volunteer whose address you can view. The plain search scopes an email
    match the same way.
- **Membership fields** (`team`, `role`): the condition only considers
  rosters you can view.
  - `team != 'X'` and `NOT (team = 'X')` mean "holds no such membership
    *that you can see*".
  - `team IS NULL` means "no visible membership at all".
  - An invisible roster can neither prove nor disprove membership.
- Comparisons with an absent value (`NULL`) never match, as in SQL.
  `field IS NULL` asks for absence explicitly.
- Admins see everything, everywhere, as usual.
- Accounts, sessions, ballots, and history are not queryable at all: those
  names do not exist in the filter language.
