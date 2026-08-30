# The workload model

Volunteer burnout in a parish has a familiar shape. The same 5 reliable
people quietly collect ministries until one of them moves away. Then the
[impact report](history.md) suddenly shows 6 holes. Workload makes that
concentration visible *before* it breaks.

## The score

A volunteer's workload score is the sum, over all their memberships, of

```
team weight × role multiplier
```

- **Team weight** (`workload_weight`, set per team) says how heavy the work
  of the ministry is. A team that an admin creates starts at 1, which is
  ordinary work, and the admin adjusts it up or down from there. If you
  clear the weight, it goes back to 0, and the team is *excluded* from
  workload scores. So membership in, for example, a team that is really a
  mailing list can cost nothing. Task-force teams borrow their rosters from
  real ministries, so their weight is always 0.
- **Role multiplier** says how much of that load the role carries. The
  defaults are leader ×3, second ×2, core ×1.5, member ×1. To lead a
  ministry is roughly 3 times the commitment of a plain member.

The score then falls into colored **bands** (defaults: green ≤ 4, amber ≤ 8,
red above). The bands appear as badges on volunteer pages and lists, and as
node colors in the graph. Multipliers, bands, and weights are all
[configurable](../how-to/custom-fields-and-workload.md). The defaults are a
start, not a doctrine.

## Worked example

Take *Liturgy* with weight 3, *Altar Society* with 1.5, and *Hospitality*
with 1. A volunteer leads the first two and serves as core in the third.
That volunteer scores 3×3 + 1.5×3 + 1×1.5 = **15**. That is deep red, a
concentration risk worth a discussion. A core member of Liturgy alone scores
3×1.5 = **4.5**, which is amber and fine. (The first case is exactly Maria
Alvarez in the seeded demo.)

## Two deliberate choices

**Scores are global, not per-team.** A leader who views their roster sees
each volunteer's *total* load across the whole parish. That total includes
ministries the leader has nothing to do with, and that is the feature. The Youth
Group leader is about to ask Maria for one more favor. That leader must see
that *other* commitments already have her in the red. (Only the score is
global; the permission rules still control who sees it.)

**Visibility is leadership-only.** Admins and the leaders and seconds of the
volunteer's own teams see workload. Core members do not, and neither does
the volunteer. It is a signal for the people who ask for the work. It is not
a leaderboard, and not a number to feel guilty about. The
[permission matrix](../reference/permissions.md#permission-matrix) encodes
this.

## Interaction with history

As-of views compute scores from *historical* memberships and weights, but
from *today's* multipliers and bands. The config lives in `app_setting`,
which is not versioned. In practice this is what you want ("how loaded was
Maria last year, by our current standard?"). But it means that band colors
in old snapshots can shift when you re-tune the thresholds. See
[History and time travel](history.md).
