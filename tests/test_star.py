"""STAR tally protocol: pure arithmetic, no database."""

import pytest

from volunteerdb.star import star_tally

pytestmark = pytest.mark.pure

A, B, C = 1, 2, 3


def test_clear_winner():
    result = star_tally([A, B], [{A: 5, B: 1}, {A: 4, B: 2}, {A: 5, B: 0}])
    assert result.totals == {A: 14, B: 3}
    assert result.finalist_ids == (A, B)
    assert result.runoff == {A: 3, B: 0}
    assert result.winner_id == A
    assert not result.tie


def test_runoff_flips_the_score_leader():
    """STAR's raison d'être: broad mild preference beats a few 5-star fans."""
    ballots = [
        {A: 5, B: 0},
        {A: 5, B: 0},
        {A: 2, B: 3},
        {A: 2, B: 3},
        {A: 2, B: 3},
    ]
    result = star_tally([A, B], ballots)
    assert result.totals == {A: 16, B: 9}
    assert result.finalist_ids == (A, B)  # ordered by scoring total
    assert result.runoff == {A: 2, B: 3}
    assert result.winner_id == B
    assert not result.tie


def test_missing_scores_count_as_zero():
    result = star_tally([A, B], [{A: 3}, {A: 1}])
    assert result.totals == {A: 4, B: 0}
    assert result.runoff == {A: 2, B: 0}
    assert result.winner_id == A


def test_equal_scores_are_no_preference():
    result = star_tally([A, B], [{A: 2, B: 2}, {A: 3, B: 1}])
    assert result.no_preference == 1
    assert result.winner_id == A


def test_runoff_tie_broken_by_higher_total():
    # one ballot prefers each finalist; A's scoring total is higher
    result = star_tally([A, B], [{A: 5, B: 2}, {A: 1, B: 2}])
    assert result.totals == {A: 6, B: 4}
    assert result.runoff == {A: 1, B: 1}
    assert result.winner_id == A
    assert not result.tie


def test_full_tie_is_reported_not_resolved():
    result = star_tally([A, B], [{A: 5, B: 0}, {A: 0, B: 5}])
    assert result.winner_id is None
    assert result.tie
    assert result.tied_ids == (A, B)


def test_finalist_cut_tie_broken_head_to_head():
    # B and C tie the scoring round at 6; head-to-head B is preferred 2-1
    ballots = [
        {A: 5, B: 3, C: 2},
        {A: 5, B: 3, C: 2},
        {A: 5, B: 0, C: 2},
    ]
    result = star_tally([A, B, C], ballots)
    assert result.totals == {A: 15, B: 6, C: 6}
    assert result.finalist_ids == (A, B)
    assert result.winner_id == A


def test_unresolvable_cut_tie_reports_the_tied_group():
    # all-zero ballots: whole field tied, head-to-head all draws
    result = star_tally([A, B, C], [{A: 0, B: 0, C: 0}])
    assert result.winner_id is None
    assert result.tie
    assert result.tied_ids == (A, B, C)
    assert result.finalist_ids is None


def test_no_candidates():
    result = star_tally([], [{A: 5}])
    assert result.totals == {}
    assert result.winner_id is None
    assert not result.tie


def test_sole_candidate_wins_even_unscored():
    result = star_tally([A], [])
    assert result.winner_id == A
    assert result.finalist_ids is None
    assert result.runoff is None
    assert not result.tie


def test_no_ballots_two_candidates_is_a_tie():
    result = star_tally([A, B], [])
    assert result.ballot_count == 0
    assert result.totals == {A: 0, B: 0}
    assert result.winner_id is None
    assert result.tie
