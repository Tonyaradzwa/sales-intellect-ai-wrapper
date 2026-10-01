"""Shared pytest fixtures.

server.py keeps the tier-1 lookup cache and the file-backed catalog cache
as module-level state (by design — they need to persist across requests
within one running process). That same persistence is a hazard across
tests in one pytest session, so this autouse fixture resets both before
every test.
"""

import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import server  # noqa: E402


def _reset_catalog_state():
    server._lookup_cache.clear()
    server._catalog_state["products"] = None
    server._catalog_state["fetched_at"] = None
    server._catalog_state["index"] = None
    server._catalog_state["checked_disk"] = False


@pytest.fixture(autouse=True)
def _reset_server_caches():
    _reset_catalog_state()
    yield
    _reset_catalog_state()
