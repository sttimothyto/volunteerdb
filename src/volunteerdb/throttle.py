"""In-process sliding-window failure throttling (the app runs single-process)."""

import time
from collections import defaultdict, deque

_hits: dict[str, deque[float]] = defaultdict(deque)
# Each key belongs to one limiter with one fixed window; remember it so the
# periodic sweep prunes every key by ITS OWN window, not whatever window the
# call that happened to trigger the sweep was using. A short-window caller must
# not evict hits that a longer-window bucket still counts.
_windows: dict[str, float] = {}

# A key is normally dropped the moment its own window empties — but that only
# happens if somebody asks about it again. Keys are attacker-supplied (an email
# address on the login throttle, an IP on the OTP one), so a stream of one-shot
# addresses would otherwise leave an entry each, forever.
# Every SWEEP_EVERY calls, walk the whole map once and drop what has gone
# quiet: a few hundred keys at parish scale, so the walk is far cheaper than
# the bookkeeping to avoid it.
SWEEP_EVERY = 512
_since_sweep = 0


def _sweep(now: float) -> None:
    for key in list(_hits):
        q = _hits[key]
        window_s = _windows.get(key)
        if window_s is not None:
            while q and now - q[0] > window_s:
                q.popleft()
        if not q:
            _hits.pop(key, None)
            _windows.pop(key, None)


def blocked(key: str, limit: int, window_s: float) -> bool:
    """True once `key` has accumulated `limit` hits inside the window."""
    global _since_sweep
    now = time.monotonic()
    _windows[key] = window_s
    _since_sweep += 1
    if _since_sweep >= SWEEP_EVERY:
        _since_sweep = 0
        _sweep(now)
    q = _hits[key]
    while q and now - q[0] > window_s:
        q.popleft()
    if not q:
        _hits.pop(key, None)
        _windows.pop(key, None)
    return len(q) >= limit


def hit(key: str) -> None:
    _hits[key].append(time.monotonic())
