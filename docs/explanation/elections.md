# Elections by nomination and vote

## The problem

When a role opens up, the natural move is to ask the person the priest
already knows. That is how the same dozen parishioners end up in charge of
everything, while equally capable people never come up. The failure has two
halves. The first is **selection by familiarity**: the candidate pool is
whoever comes to mind. The second is **invisible overlap**: nobody checks
what the familiar person already carries before they add more.

Armies solved both long ago. Assignments go through a central process that
sees everyone's current taskings. No commander double-tasks a soldier,
because the system shows the load before anyone cuts the order. The
elections pipeline borrows exactly that. It builds a deliberate candidate
pool with more eyes than one, and it shows each candidate's **current
commitments at the moment of decision**. Managers also see the
leadership-only [workload](workload.md) signal for their own people.

## The pipeline

A manager opens a **proposal** for one (team, role) seat, usually from the
*Vacancies* list. It carries candidates (each with the nominator's "why
them?" note), a **voting roll**, and two dates:

1. **Nominating** — until the nomination deadline (inclusive), voting
   members and managers can add candidates. Managers can edit the roll
   itself in this phase only.
2. **Voting** — from then until the voting deadline (inclusive), every
   voting member with an account scores every candidate 0–5. Voters can
   revise their ballots until the deadline. The candidate set and the roll
   do not change.
3. **Awaiting decision** — after the voting deadline the tally becomes
   visible. A manager then **appoints** a candidate, starts a **new round**,
   or cancels. The appointment creates or upgrades the membership, exactly
   like a direct roster edit.

The default roll is a template, not a rule. It holds the target team's
leader, second-in-command, and core members, plus every member of the
**clergy team**, the team named **Clergy**. Managers adjust it per proposal
while nominations are open.

The clergy team is the only team that votes on *every* proposal. Everyone
else joins a roll because of the seat in question: the leadership and core
of the team that fills it. The clergy sit on all of them because the
appointment is finally the pastor's act (see below). So the app must consult
the people who make that act on every seat, not invite them seat by seat.

The name *is* the standing; there is no `clergy_team_id` to set. This is a
deliberate choice about where a rule this quiet must live. A stored id can
drift from what it means. Point it at the wrong team and nothing looks
wrong, because rolls still fill and ballots still tally. The only symptom is
that the right people had no say, a fact that surfaces, if at all, after the
appointment.

To guard a stored id against that drift took a validator on the setting plus
rename and delete guards on the team. That is 3 doors to keep locked, and
each is a place where somebody can forget the invariant. To resolve the
name when the app builds the roll removes the drift instead of a police
action against it. There is no second copy of the answer, so the two cannot
disagree.

The cost is that a rename quietly changes who votes on the *next* proposal.
That is why the manual documents a rename as the intended way to retire the
standing, and does not treat it as an accident. See
[Configuration stored in the database](../reference/configuration.md).

## The Ignatian frame

The pipeline deliberately mirrors the Ignatian communal election: **pray
separately, vote separately, then debate together — and repeat**. That is
why voters cast ballots in private with no live totals on show, and why the
tally appears only after the vote closes. It is also why the result is
**advisory**: the appointment is a separate, human act. And "start a new
round" (same candidates and roll, fresh deadlines, no ballots) exists
precisely for the *repeat* after the debate.

## Why STAR voting

STAR — *Score Then Automatic Runoff* — has voters score every candidate 0–5
on their own merits. The two highest totals enter an automatic runoff. The
finalist that more ballots prefer (scored higher) wins it.

Against first-past-the-post this removes the **spoiler effect**. Two
similar, well-liked candidates do not split a camp's vote and hand the seat
to a third, because a score is not either/or. The runoff then keeps a small
intense faction from a win over a broad majority preference on raw points
alone.

The tie rules live in `src/volunteerdb/star.py`, pure and unit-tested. A
head-to-head count among the tied candidates breaks a tie at the finalist
cut. A runoff tie goes to the higher score total. The app **reports**
anything still tied **as a tie**; it does not flip a coin. That is
acceptable because the tally only advises the appointment.

## Ballot secrecy, and its limits

Individual scores never leave the service layer. The API and GUI expose a
voter's own ballot to its owner, per-voter *has-voted* flags for turnout,
and post-conclusion aggregates. They expose nothing else, admins included.
`audit.REDACTED_COLUMNS` lists the `score` column, so audit logs record
that somebody wrote a ballot but never its values
(`tests/test_schema_invariants.py` enforces this).

The honest boundary: scores are ordinary rows in `proposal_ballot`, so
anyone with raw database access, or a database backup, can read them. For a
parish tool the design accepts that trade-off; the secrecy target is the
application surface, not the DBA.

## Deadlines without a scheduler

Nothing runs on a clock. Deadlines are plain dates, and the app *derives* an
open proposal's phase on every read. The phase is nominating while today ≤
nomination deadline, voting while today ≤ voting deadline, and concluded
after. The app
computes "today" in the parish's timezone (`VDB_TIMEZONE`, default
`America/Toronto`). So a deadline means "through the end of that day at the
parish", not in UTC.

Services enforce phases on writes, so a late ballot fails no matter what the
page showed a moment earlier. Managers can move deadlines while a proposal
is open, to close the vote early or to extend it. There is one guard:
nominations cannot reopen once ballots exist, because the candidate set
under a cast ballot must not change.

The costs of this choice are deliberate: phase changes happen "lazily" at
read time, and the application never emails anyone of its own accord. The
one concession is the **nightly digest** (`jobs.proposal_digest`). The
in-app scheduler (`volunteerdb.scheduler`) runs it at 03:30 parish time.

Each voter gets at most one email per night. It covers everything that
changed for them: a roll that now includes them, or a proposal that entered
its voting phase. It restates both deadlines. A row in `notification` per
(voter, stage) makes each notice one-shot; a failed send writes nothing and
retries the next night. There are no reminders when a deadline nears, and
nothing goes out by email at appointment.

## What it replaced

The first version of this feature shipped a lighter flow: one volunteer
proposed per (team, role), and the manager accepted or declined directly. It
solved double-entry but not the familiarity problem. The proposer still
picked one name from memory, and one manager decided alone. The
nomination-and-vote flow replaced it outright and rebuilt the table rather
than keep both paths, so that *every* appointment passes through the same
deliberate gate. (The old shape survives nowhere in the repo now that the
migration chain is squashed. That is the intended trade: the schema is
easier to read, and one abandoned data model is no longer part of it.)
