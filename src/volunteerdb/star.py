"""STAR voting tally: Score, Then Automatic Runoff.

Pure arithmetic — no database, no framework — so the whole protocol is
unit-testable in isolation. A ballot maps candidate id -> score 0-5; a
missing entry counts as 0.

1. Scoring round: sum each candidate's scores; the two highest totals
   become the finalists. A tie at the finalist cut is broken head-to-head
   among the tied candidates (most pairwise wins advances); if that still
   cannot fill the pair, the tally reports a tie instead of guessing.
2. Automatic runoff: each ballot counts as one vote for the finalist it
   scored higher (equal scores express no preference); the finalist
   preferred on more ballots wins. A runoff tie goes to the higher
   scoring-round total; if the totals match too, the tally reports a tie.

The result is advisory (the appointment remains a human act), so a
reported tie is an acceptable outcome, not an error.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class StarResult:
    ballot_count: int
    totals: dict[int, int]  # candidate id -> scoring-round sum
    finalist_ids: tuple[int, int] | None  # None: <2 candidates or unresolved cut
    runoff: dict[int, int] | None  # finalist id -> ballots preferring them
    no_preference: int | None  # ballots scoring both finalists equally
    winner_id: int | None  # None: no candidates, or reported tie
    tie: bool  # the protocol could not pick a single winner
    tied_ids: tuple[int, ...] = ()  # who remains tied, for display


def _prefer_counts(
    ballots: Sequence[Mapping[int, int]], a: int, b: int
) -> tuple[int, int]:
    """(# ballots scoring a over b, # ballots scoring b over a)."""
    a_pref = b_pref = 0
    for ballot in ballots:
        score_a, score_b = ballot.get(a, 0), ballot.get(b, 0)
        if score_a > score_b:
            a_pref += 1
        elif score_b > score_a:
            b_pref += 1
    return a_pref, b_pref


def _fill_from_tier(
    tier: list[int], ballots: Sequence[Mapping[int, int]], need: int
) -> tuple[list[int] | None, tuple[int, ...]]:
    """Pick `need` finalists from a scoring tier bigger than the seats left.

    Round-robin head-to-head: a pairwise win scores 2, a draw 1 apiece.
    Returns (chosen, ()) — or (None, tied_ids) when the last seat is still
    contested at equal pairwise scores.
    """
    wins = dict.fromkeys(tier, 0)
    for i, a in enumerate(tier):
        for b in tier[i + 1 :]:
            a_pref, b_pref = _prefer_counts(ballots, a, b)
            if a_pref > b_pref:
                wins[a] += 2
            elif b_pref > a_pref:
                wins[b] += 2
            else:
                wins[a] += 1
                wins[b] += 1
    ranked = sorted(tier, key=lambda c: (-wins[c], c))
    boundary = wins[ranked[need - 1]]
    if wins[ranked[need]] == boundary:
        return None, tuple(c for c in ranked if wins[c] == boundary)
    return ranked[:need], ()


def star_tally(
    candidate_ids: Sequence[int], ballots: Sequence[Mapping[int, int]]
) -> StarResult:
    candidates = list(dict.fromkeys(candidate_ids))
    totals = {c: sum(b.get(c, 0) for b in ballots) for c in candidates}
    count = len(ballots)
    if not candidates:
        return StarResult(count, totals, None, None, None, None, False)
    if len(candidates) == 1:
        return StarResult(count, totals, None, None, None, candidates[0], False)

    finalists: list[int] = []
    for total in sorted(set(totals.values()), reverse=True):
        tier = sorted(c for c in candidates if totals[c] == total)
        if len(finalists) + len(tier) <= 2:
            finalists.extend(tier)
        else:
            chosen, tied = _fill_from_tier(tier, ballots, 2 - len(finalists))
            if chosen is None:
                return StarResult(count, totals, None, None, None, None, True, tied)
            finalists.extend(chosen)
        if len(finalists) == 2:
            break

    finalists.sort(key=lambda c: (-totals[c], c))
    first, second = finalists
    first_pref, second_pref = _prefer_counts(ballots, first, second)
    runoff = {first: first_pref, second: second_pref}
    no_preference = count - first_pref - second_pref
    pair = (first, second)
    if first_pref != second_pref:
        winner = first if first_pref > second_pref else second
        return StarResult(count, totals, pair, runoff, no_preference, winner, False)
    if totals[first] != totals[second]:
        winner = first if totals[first] > totals[second] else second
        return StarResult(count, totals, pair, runoff, no_preference, winner, False)
    return StarResult(count, totals, pair, runoff, no_preference, None, True, pair)
