"""A small O(1) LFU (least-frequently-used) cache.

Used to remember previously-resolved product-name lookups (see
_find_cached_matches in server.py) so a name that recurs often in daily
deliveries skips both the catalog search and the LLM agent on repeat.
"""

from collections import OrderedDict


class LFUCache:
    def __init__(self, maxsize):
        self.maxsize = maxsize
        self._val = {}
        self._freq = {}
        self._by_freq = {}
        self._min_freq = 0

    def get(self, key):
        if key not in self._val:
            return None
        self._bump(key)
        return self._val[key]

    def put(self, key, value):
        if self.maxsize <= 0:
            return
        if key in self._val:
            self._val[key] = value
            self._bump(key)
            return
        if len(self._val) >= self.maxsize:
            self._evict()
        self._val[key] = value
        self._freq[key] = 1
        self._by_freq.setdefault(1, OrderedDict())[key] = None
        self._min_freq = 1

    def clear(self):
        self._val.clear()
        self._freq.clear()
        self._by_freq.clear()
        self._min_freq = 0

    def __len__(self):
        return len(self._val)

    def __contains__(self, key):
        return key in self._val

    def _bump(self, key):
        freq = self._freq[key]
        del self._by_freq[freq][key]
        if not self._by_freq[freq]:
            del self._by_freq[freq]
            if self._min_freq == freq:
                self._min_freq += 1
        new_freq = freq + 1
        self._freq[key] = new_freq
        self._by_freq.setdefault(new_freq, OrderedDict())[key] = None

    def _evict(self):
        # Oldest key among the least-frequently-used (front of that freq's
        # insertion-ordered dict) is the eviction victim.
        keys = self._by_freq[self._min_freq]
        evict_key, _ = keys.popitem(last=False)
        if not keys:
            del self._by_freq[self._min_freq]
        del self._val[evict_key]
        del self._freq[evict_key]
