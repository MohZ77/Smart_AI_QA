from __future__ import annotations

import time

from huixiaoan.cache import TTLCache


def test_ttl_and_lru() -> None:
    now = [0.0]
    cache = TTLCache(max_entries=2, ttl_s=1.0, clock=lambda: now[0])
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.get("a") == 1
    cache.set("c", 3)
    assert cache.get("b") is None
    now[0] = 2.0
    assert cache.get("a") is None
