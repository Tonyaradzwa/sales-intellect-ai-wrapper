"""Unit tests for the LFUCache used as the server's tier-1 lookup cache."""

import sys
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
