"""Sliding-window rate limits as arithmetic over a Ledger: no clock, no sleeps,
no module state -- `now` is an argument and every hit returns a new ledger."""

from datetime import UTC, datetime, timedelta

import pytest

from volunteerdb import throttle
from volunteerdb.env import ThrottleCell

pytestmark = pytest.mark.pure

T0 = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def _hits(ledger: throttle.Ledger, key: str, n: int, at: datetime) -> throttle.Ledger:
    for _ in range(n):
        ledger = throttle.hit(ledger, key, at)
    return ledger


def test_blocked_once_the_family_limit_is_reached():
    limit = throttle.LIMITS["pw"]
    ledger = _hits(throttle.Ledger(), "pw:a@example.org", limit.hits - 1, T0)
    assert not throttle.blocked(ledger, "pw:a@example.org", T0)
    ledger = throttle.hit(ledger, "pw:a@example.org", T0)
    assert throttle.blocked(ledger, "pw:a@example.org", T0)


def test_the_window_slides():
    limit = throttle.LIMITS["pw"]
    ledger = _hits(throttle.Ledger(), "pw:a@example.org", limit.hits, T0)
    assert throttle.blocked(
        ledger, "pw:a@example.org", T0 + limit.window - timedelta(seconds=1)
    )
    assert not throttle.blocked(
        ledger, "pw:a@example.org", T0 + limit.window + timedelta(seconds=1)
    )


def test_keys_are_independent_and_families_differ():
    ledger = _hits(throttle.Ledger(), "pw:a@example.org", 5, T0)
    assert throttle.blocked(ledger, "pw:a@example.org", T0)
    assert not throttle.blocked(ledger, "pw:b@example.org", T0)
    assert not throttle.blocked(ledger, "pw-ip:1.2.3.4", T0), "30 per address, not 5"
    assert throttle.limit_for("sub-req:7").window == timedelta(days=1)


def test_hit_returns_a_new_ledger_and_leaves_the_old_one_alone():
    before = throttle.Ledger()
    after = throttle.hit(before, "otp-ip:1.2.3.4", T0)
    assert before.hits == {} and after.hits == {"otp-ip:1.2.3.4": (T0,)}


def test_a_hit_drops_this_keys_expired_entries_on_the_way():
    ledger = _hits(throttle.Ledger(), "pw:a@example.org", 5, T0)
    later = T0 + timedelta(hours=1)
    ledger = throttle.hit(ledger, "pw:a@example.org", later)
    assert ledger.hits["pw:a@example.org"] == (later,)


def test_prune_forgets_keys_with_nothing_live():
    ledger = _hits(throttle.Ledger(), "pw:gone@example.org", 3, T0)
    ledger = throttle.hit(ledger, "otp-ip:9.9.9.9", T0 + timedelta(minutes=30))
    pruned = throttle.prune(ledger, T0 + timedelta(minutes=40))
    assert set(pruned.hits) == {"otp-ip:9.9.9.9"}, (
        "the 15-minute family expired, the hour one did not"
    )


def test_an_unknown_family_is_a_programming_error():
    with pytest.raises(KeyError):
        throttle.blocked(throttle.Ledger(), "mystery:1", T0)


def test_the_cell_holds_one_ledger_and_sweeps_on_its_own_cadence():
    cell = ThrottleCell()
    cell.SWEEP_EVERY = 3
    cell.hit("pw:stale@example.org", T0)
    assert cell.blocked("pw:stale@example.org", T0) is False
    later = T0 + timedelta(hours=2)
    cell.hit("otp-ip:1.1.1.1", later)
    cell.hit("otp-ip:1.1.1.1", later)  # the third hit sweeps
    assert set(cell.snapshot().hits) == {"otp-ip:1.1.1.1"}
    cell.reset()
    assert cell.snapshot().hits == {}


@pytest.mark.parametrize("family", sorted(throttle.LIMITS))
def test_the_window_closes_at_exactly_its_length(family):
    """A hit taken at T0 counts until, but not at, T0 + window: `_live` keeps
    hits strictly after the cutoff, so at the instant the window ends the key
    is free again."""
    key = f"{family}:edge"
    limit = throttle.LIMITS[family]
    ledger = _hits(throttle.Ledger(), key, limit.hits, T0)
    assert throttle.blocked(ledger, key, T0 + limit.window - timedelta(microseconds=1))
    assert not throttle.blocked(ledger, key, T0 + limit.window)
