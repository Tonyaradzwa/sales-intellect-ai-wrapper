"""Unit tests for the LFUCache used as the server's tier-1 lookup cache."""

import sys
import threading
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

from lfu_cache import LFUCache  # noqa: E402


def test_get_miss_returns_none():
    cache = LFUCache(3)
    assert cache.get("missing") is None


def test_put_then_get():
    cache = LFUCache(3)
    cache.put("a", 1)
    assert cache.get("a") == 1


def test_evicts_least_frequently_used():
    cache = LFUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.get("a")  # a: freq 2, b: freq 1
    cache.put("c", 3)  # evicts b (lowest freq)

    assert cache.get("b") is None
    assert cache.get("a") == 1
    assert cache.get("c") == 3


def test_tie_in_frequency_evicts_oldest_inserted():
    cache = LFUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    # Both at freq 1 — "a" was inserted first, so it's evicted first.
    cache.put("c", 3)

    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3


def test_put_on_existing_key_updates_value_without_eviction():
    cache = LFUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("a", 100)

    assert cache.get("a") == 100
    assert cache.get("b") == 2
    assert len(cache) == 2


def test_respects_size_70():
    cache = LFUCache(70)
    for i in range(100):
        cache.put(f"k{i}", i)

    assert len(cache) == 70
    # The 30 oldest, never-reaccessed keys should have been evicted.
    for i in range(30):
        assert cache.get(f"k{i}") is None
    # The most recent 70 all survive.
    for i in range(30, 100):
        assert cache.get(f"k{i}") == i


def test_clear_empties_cache():
    cache = LFUCache(3)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.clear()

    assert len(cache) == 0
    assert cache.get("a") is None


def test_merge_combines_existing_and_new_entries():
    cache = LFUCache(10)
    cache.put("k", [1, 2])
    cache.merge("k", [3, 4])
    assert cache.get("k") == [1, 2, 3, 4]


def test_merge_on_missing_key_acts_like_put():
    cache = LFUCache(10)
    cache.merge("k", [1])
    assert cache.get("k") == [1]


def test_concurrent_put_get_merge_does_not_corrupt_cache():
    """Regression test for server.py now running parse work in background
    threads (one per in-flight GRN paste), which means concurrent access to
    this per-process cache is a real scenario, not a theoretical one — this
    hammers put/get/merge from many threads and asserts the cache survives
    (no exception, no size overrun), without asserting exact contents since
    concurrent writes to overlapping keys don't have one "correct" outcome.
    """
    cache = LFUCache(20)
    errors = []

    def worker(n):
        try:
            for i in range(200):
                key = f"k{i % 25}"
                if n % 3 == 0:
                    cache.put(key, [n, i])
                elif n % 3 == 1:
                    cache.merge(key, [n, i])
                else:
                    cache.get(key)
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(cache) <= 20
