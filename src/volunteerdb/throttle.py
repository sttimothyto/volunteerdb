"""In-process sliding-window failure throttling (the app runs single-process)."""

import time
from collections import defaultdict, deque

_hits: dict[str, deque[float]] = defaultdict(deque)


def blocked(key: str, limit: int, window_s: float) -> bool:
    """True once `key` has accumulated `limit` hits inside the window."""
    now = time.monotonic()
    q = _hits[key]
    while q and now - q[0] > window_s:
        q.popleft()
    if not q:
        _hits.pop(key, None)
    return len(q) >= limit


def hit(key: str) -> None:
    _hits[key].append(time.monotonic())
