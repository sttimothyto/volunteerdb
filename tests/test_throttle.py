"""Sliding-window throttle unit tests with a fake clock (no sleeps)."""

import pytest

from volunteerdb import throttle


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(throttle.time, "monotonic", fake)
    return fake


def test_blocked_after_limit_hits(clock):
    for _ in range(2):
        throttle.hit("k")
    assert throttle.blocked("k", limit=3, window_s=60) is False
    throttle.hit("k")
    assert throttle.blocked("k", limit=3, window_s=60) is True


def test_window_expiry_unblocks(clock):
    for _ in range(3):
        throttle.hit("k")
    assert throttle.blocked("k", limit=3, window_s=60) is True
    clock.advance(61)
    assert throttle.blocked("k", limit=3, window_s=60) is False


def test_keys_are_independent(clock):
    for _ in range(5):
        throttle.hit("a")
    assert throttle.blocked("a", limit=5, window_s=60) is True
    assert throttle.blocked("b", limit=5, window_s=60) is False


def test_stale_key_removed_from_state(clock):
    throttle.hit("k")
    assert "k" in throttle._hits
    clock.advance(61)
    throttle.blocked("k", limit=1, window_s=60)
    assert "k" not in throttle._hits, "expired keys are pruned, not retained forever"
