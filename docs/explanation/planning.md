# Planning by nomination and vote

## The problem

When a role opens up, the natural move is to ask the person the priest
already knows — which is how the same dozen parishioners end up running
everything while equally capable people are never considered. The failure
has two halves: **selection by familiarity** (the candidate pool is
whoever comes to mind) and **invisible overlap** (nobody checks what the
familiar person is already carrying before adding more).

Armies solved both long ago: assignments go through a manning process
with central visibility of everyone's current taskings, and no commander
double-tasks a soldier because the system shows the load before the order
is cut. The planning pipeline borrows exactly that: a deliberate candidate
pool built by more eyes than one, and each candidate's **current
commitments displayed at the moment of decision** (managers additionally
see the admin-only [workload](workload.md) signal).

## The pipeline

A manager opens a **proposal** for one (team, role) seat — usually from
the Vacancies list. It carries candidates (each with the nominator's "why
them?" note), a **voting roll**, and two dates:

1. **Nominating** — until the nomination deadline (inclusive), voting
   members and managers may add candidates. The roll itself may be edited
   by managers in this phase only.
2. **Voting** — from then until the voting deadline (inclusive), every
   voting member with an account scores every candidate 0–5. Ballots can
   be revised until the deadline; the candidate set and roll are frozen.
3. **Awaiting decision** — after the voting deadline the tally becomes
   visible, and a manager **appoints** a candidate (creating or upgrading
   the membership, exactly like editing the roster directly), starts a
   **new round**, or cancels.

The default roll is a template, not a rule: the target team's leader,
second-in-command, and core members, plus every member of the configured
**clergy team** (an `app_setting`, edited by admins on `/planning`).
Managers adjust it per proposal while nominations are open.

The clergy team — always the team named **Clergy** — is the only team that
votes on *every* proposal. Everyone else joins a roll because of the seat
in question: the leadership and core of the team that is actually filling
it. The clergy sit on all of them because the appointment is finally the
pastor's act (see below), so the people who make that act must be
consulted on every seat, not invited seat by seat. Only one team can hold
this standing: the setting is a single `clergy_team_id`, not a list.

That the team is *named* **Clergy** is enforced rather than trusted, which
is a deliberate choice about where a rule this quiet should live. Nothing
about a wrong clergy team looks wrong: rolls still fill, ballots still
tally, and the only symptom is that the people who should have been
consulted were not — a fact that surfaces, if at all, after the
appointment. So the name is checked when the setting is saved, and the team
is protected from being renamed or deleted while it holds the role. See
[Configuration stored in the database](../reference/configuration.md) for
how to move the role when a parish genuinely needs to.

## The Ignatian frame

The pipeline deliberately mirrors the Ignatian communal election: **pray
separately, vote separately, then debate together — and repeat**. That is
why ballots are cast privately with no running totals shown, why the tally
appears only after voting closes, and why the result is **advisory**: the
appointment is a separate, human act, and "start a new round" (same
candidates and roll, fresh deadlines, no ballots) exists precisely for the
*repeat* after the debate.

## Why STAR voting

STAR — *Score Then Automatic Runoff* — has voters score every candidate
0–5 on their own merits. The two highest totals enter an automatic runoff,
won by the finalist preferred (scored higher) on more ballots.

Against first-past-the-post this removes the **spoiler effect**: two
similar, well-liked candidates do not split a camp's vote and hand the
seat to a third, because scoring is not either/or. The runoff then keeps a
small intense faction from beating a broad majority preference on raw
points alone.

Tie handling (implemented in `src/volunteerdb/star.py`, pure and
unit-tested): a tie at the finalist cut is broken head-to-head among the
tied candidates; a runoff tie goes to the higher score total; anything
still tied is **reported as a tie**, not coin-flipped — acceptable
because the tally only advises the appointment.

## Ballot secrecy, and its limits

Individual scores never leave the service layer: the API and GUI expose a
voter's own ballot to its owner, per-voter *has-voted* flags for turnout,
and post-conclusion aggregates — nothing else, for admins included. The
`score` column is registered in `audit.REDACTED_COLUMNS`, so audit logs
record that a ballot was written but never its values (enforced by
`tests/test_schema_invariants.py`).

The honest boundary: scores are ordinary rows in `proposal_ballot`, so
anyone with raw database access — or a database backup — can read them.
For a parish tool that trade-off is accepted; the secrecy target is the
application surface, not the DBA.

## Deadlines without a scheduler

Nothing runs on a clock. Deadlines are plain dates and an open proposal's
phase is *derived* on every read: nominating while today ≤ nomination
deadline, voting while today ≤ voting deadline, concluded after. "Today"
is computed in the parish's timezone (`VDB_TIMEZONE`, default
`America/Toronto`), so a deadline means "through the end of that day at
the parish", not in UTC. Services enforce phases on writes, so a late
ballot fails no matter what the page showed a moment earlier; managers
can move deadlines while a proposal is open (concluding voting early, or
extending it), with one guard: nominations cannot reopen once ballots
exist, because the candidate set under a cast ballot must not change.

The costs of this choice are deliberate: nobody is emailed when voting
opens or a deadline nears (voters must check the page), and phase changes
happen "lazily" at read time. If reminders prove necessary, the
host-crontab pattern used for backups is the intended home for them.

## What it replaced

Revision `0007` shipped a lighter flow — one volunteer proposed per
(team, role), accepted or declined directly by the manager. It solved
double-entry but not the familiarity problem: the proposer still picked
one name from memory and one manager decided alone. Revision `0008`
replaced it (the table was rebuilt; the old flow's data model is kept
only in that migration's `downgrade()`) rather than keeping both, so
that *every* appointment passes through the same deliberate gate.
