# Search query filters

The search boxes on the dashboard, the volunteers list, and the teams list
accept SQL `WHERE`-clause filters alongside plain text. Anything that does
not read as a complete boolean condition — a name, a half-typed clause, a
sentence — is treated as the usual substring search; a well-formed filter
with a problem (an unknown field, a bad value) reports the problem instead
of quietly searching for the text.

The filter is parsed and compiled into the same permission-checked queries
the pages already run — it is a search syntax, not database access.

```text
phone LIKE '555%' AND team = 'Liturgy'
years_served > 2 AND role IN ('leader', 'second')
NOT (notes IS NULL)
created > '2025-01-01'
```

## Syntax

- Combine conditions with `AND`, `OR`, `NOT`, and parentheses.
- Compare with `=`, `!=`, `<`, `<=`, `>`, `>=`, `LIKE`, `ILIKE` (`%` any
  run, `_` one character; `ILIKE` ignores case), `IN (…)`,
  `BETWEEN … AND …`, and `IS [NOT] NULL`.
- One side of a comparison is a field, the other a value: quoted text,
  a number, or `true`/`false`. Functions, subqueries, and field-to-field
  comparisons are not supported.
- Dates and timestamps are quoted ISO 8601 (`'2026-08-17'`,
  `'2026-08-17 10:30'`, offsets like `'+02:00'`/`'Z'` where the field
  carries a zone); durations are quoted ISO 8601 like `'P1DT2H30M'`;
  decimals and integers are bare numbers.

## Volunteer fields (dashboard and volunteers list)

| Field | Type | Notes |
|---|---|---|
| `name`, `first_name`, `last_name` | text | `name` is "First Last" |
| `email` | text | matches for everyone, like the plain search |
| `id` | integer | |
| `created` | timestamp with zone | when the record was created — there is no `joined` field (membership dates are not tracked) |
| `is_active` | true/false | inactive volunteers stay hidden from non-admins regardless |
| `phone`, `notes` | text | private — see below |
| `team` | text | the team's name; membership condition |
| `role` | text | `leader` · `second` · `core` · `member` |
| `custom.<key>` | the field's own type | any custom field by its key (the bare key works too unless it collides with a built-in); private |

## Team fields (teams list)

`name`, `path`, `description` (text), `is_active` (true/false), and the
coverage counts `leaders`, `seconds`, `core`, `members`, `total`, `gaps`
(integers). Counts are only readable where the page shows them — for teams
you do not manage they never match. Query matches keep their ancestor rows,
exactly like the substring filter, so the tree indent stays intact.

## What each role's filter can see

Filters obey the same visibility rules as everything else; a filter cannot
be used to learn a value the pages would redact.

- **Private fields** (`phone`, `notes`, custom values): a condition on them
  is *false* for every volunteer whose private fields you cannot view
  (yourself, plus teams where you are leader, second, or core — the same
  scope as the plain search). That holds under `NOT` too:
  `NOT (phone LIKE '555%')` matches, at most, the same people whose phones
  you could read.
- **Membership fields** (`team`, `role`): the condition only considers
  rosters you may view. `team != 'X'` and `NOT (team = 'X')` mean "holds no
  such membership *that you can see*", and `team IS NULL` means "no visible
  membership at all" — an invisible roster can neither prove nor disprove
  membership.
- Comparisons with an absent value (`NULL`) never match, as in SQL;
  `field IS NULL` asks for absence explicitly.
- Admins see everything, everywhere, as usual.

Accounts, sessions, ballots, and history are not queryable at all — those
names simply do not exist in the filter language.
