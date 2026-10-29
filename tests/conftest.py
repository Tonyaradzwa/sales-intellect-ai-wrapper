"""Shared pytest fixtures.

server.py keeps the tier-1 lookup cache and the file-backed catalog cache
as module-level state (by design — they need to persist across requests
within one running process). That same persistence is a hazard across
tests in one pytest session, so this autouse fixture resets both before
every test.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

# Before anything imports events.py: tests must not write to the real events.log.
os.environ.setdefault("SI_EVENTS_LOG", os.path.join(tempfile.mkdtemp(), "events.log"))

os.environ.setdefault("SI_LAST_GRN_PATH", os.path.join(tempfile.mkdtemp(), "last_grn.json"))

import server  # noqa: E402


def _reset_catalog_state():
    server._lookup_cache.clear()
    server._catalog_state["products"] = None
    server._catalog_state["fetched_at"] = None
    server._catalog_state["index"] = None
    server._catalog_state["cache_mtime"] = server._CACHE_MTIME_UNSET


@pytest.fixture(autouse=True)
def _reset_server_caches():
    _reset_catalog_state()
    # The "previous GRN" record is shared state on disk; each test starts with none.
    for suffix in ("", ".lock"):
        try:
            os.remove(server.grn_store.STORE_PATH + suffix)
        except FileNotFoundError:
            pass
    yield
    _reset_catalog_state()
