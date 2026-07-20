# The capacity model

Volunteer burnout in a parish has a familiar shape: the same five reliable
people quietly accumulate ministries until one of them moves away — and the
[impact report](history.md) suddenly shows six holes. Capacity makes that
accumulation visible *before* it breaks.

## The score

A volunteer's workload score is the sum, over all their memberships, of

```
team weight × role multiplier
```

- **Team weight** (`workload_weight`, set per team) says how demanding the
  ministry is. It is optional — unweighted teams count 0 — so the parish
  only quantifies the ministries that represent real load, and membership
  in, say, a mailing-list-like team costs nothing.
- **Role multiplier** says how much of that load the role carries.
  Defaults: leader ×3, second ×2, core ×1.5, member ×1 — leading a ministry
  is roughly three times the commitment of showing up for it.

The score is then bucketed into colored **bands** (defaults: green ≤ 4,
amber ≤ 8, red above) that appear as badges on volunteer pages and lists
and as node colors in the graph. Multipliers, bands, and weights are all
[configurable](../how-to/custom-fields-and-capacity.md); the defaults are a
starting point, not a doctrine.

## Worked example

With *Liturgy* weighted 3, *Altar Society* 1.5, and *Hospitality* 1: a
volunteer who leads the first two and helps as core in the third scores
3×3 + 1.5×3 + 1×1.5 = **15** — deep red, a concentration risk worth
discussing. A core member of Liturgy alone scores 3×1.5 = **4.5** — amber,
fine. (The first is exactly the seeded demo's Maria Alvarez.)

## Two deliberate choices

**Scores are global, not per-team.** An admin viewing any roster sees each
volunteer's *total* load across the whole parish — every weighted ministry
counts. That is the feature: whoever is about to ask Maria for one more
favor should see that *other* commitments already have her in the red.
(Only the score is global; who may see it is still gated.)

**Visibility is admin-only.** Only admins see capacity; team leaders and
seconds don't, core members don't, and neither does the volunteer themself.
It is a planning signal for the people overseeing the whole parish — not a
leaderboard, and not a number to feel guilty about. The
[permission matrix](../reference/permissions.md#permission-matrix) encodes
this.

## Interaction with history

As-of views compute scores from *historical* memberships and weights but
*today's* multipliers and bands — the config lives in `app_setting`, which
is not versioned. In practice this is what you want ("how loaded was Maria
last year, by our current standard?"), but it means band colors in old
snapshots can shift when you re-tune thresholds. See
[History and time travel](history.md).
