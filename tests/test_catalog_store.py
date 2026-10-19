"""Tests for server.py's file-backed catalog cache: get_catalog() must never
fetch live on its own, and refresh_catalog() is the only path that does.
"""

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import server  # noqa: E402


def _no_cache_file(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CATALOG_CACHE_PATH", str(tmp_path / "catalog_cache.json"))


def test_get_catalog_does_not_fetch_when_no_cache_file(tmp_path, monkeypatch):
    _no_cache_file(tmp_path, monkeypatch)
    calls = {"n": 0}
    monkeypatch.setattr(server.si_client, "list_products", lambda: calls.__setitem__("n", calls["n"] + 1) or [])

    products, fetched_at = server.get_catalog()

    assert products is None
    assert fetched_at is None
    assert calls["n"] == 0


def test_get_catalog_index_is_none_when_no_cache_file(tmp_path, monkeypatch):
    _no_cache_file(tmp_path, monkeypatch)
    monkeypatch.setattr(server.si_client, "list_products", lambda: [])

    assert server.get_catalog_index() is None


def test_refresh_catalog_fetches_live_and_writes_file(tmp_path, monkeypatch):
    _no_cache_file(tmp_path, monkeypatch)
    calls = {"n": 0}

    def fake_list_products():
        calls["n"] += 1
        return [{"id": "p1", "product_name": "Widget", "product_code": "W1", "cost": 5}]

    monkeypatch.setattr(server.si_client, "list_products", fake_list_products)

    products, fetched_at = server.refresh_catalog()

    assert calls["n"] == 1
    assert products == [{"product_id": "p1", "name": "Widget", "code": "W1", "cost": 5}]
    assert fetched_at is not None

    import os
    assert os.path.exists(server.CATALOG_CACHE_PATH)


def test_get_catalog_loads_from_file_without_fetching_once_written(tmp_path, monkeypatch):
    _no_cache_file(tmp_path, monkeypatch)
    calls = {"n": 0}

    def fake_list_products():
        calls["n"] += 1
        return [{"id": "p1", "product_name": "Widget", "product_code": "W1", "cost": 5}]

    monkeypatch.setattr(server.si_client, "list_products", fake_list_products)

    server.refresh_catalog()
    assert calls["n"] == 1

    # Simulate a fresh process picking the file back up.
    server._catalog_state["products"] = None
    server._catalog_state["fetched_at"] = None
    server._catalog_state["index"] = None
    server._catalog_state["cache_mtime"] = server._CACHE_MTIME_UNSET

    products, _ = server.get_catalog()

    assert calls["n"] == 1  # no second API call
    assert products[0]["product_id"] == "p1"


def test_get_catalog_does_not_reread_file_when_mtime_unchanged(tmp_path, monkeypatch):
    """get_catalog() stats the file on every call, but only re-reads/parses
    it when the mtime actually changed since this process last saw it."""
    _no_cache_file(tmp_path, monkeypatch)
    load_calls = {"n": 0}
    orig_load = server._load_catalog_from_file

    def counting_load():
        load_calls["n"] += 1
        return orig_load()

    monkeypatch.setattr(server, "_load_catalog_from_file", counting_load)
    monkeypatch.setattr(server.si_client, "list_products", lambda: [])
    server.refresh_catalog()
    assert load_calls["n"] == 0  # refresh writes + loads in-memory directly

    server.get_catalog()
    server.get_catalog()
    server.get_catalog()

    assert load_calls["n"] == 0  # mtime never changed since refresh_catalog set it


def test_get_catalog_picks_up_a_change_made_by_another_process(tmp_path, monkeypatch):
    """Simulates the multi-worker case: this process loads the catalog, then
    a *different* worker process refreshes the file on disk (mtime changes).
    The next get_catalog() call in THIS process must notice and reload —
    not keep serving its old in-memory copy, and not need a restart."""
    _no_cache_file(tmp_path, monkeypatch)
    monkeypatch.setattr(server.si_client, "list_products", lambda: [
        {"id": "p1", "product_name": "Widget", "product_code": "W1", "cost": 5}
    ])
    server.refresh_catalog()
    products, _ = server.get_catalog()
    assert products[0]["product_id"] == "p1"
    server._lookup_cache.put("widget", [{"matched_product_id": "p1", "name": "Widget", "code": "W1"}])

    # Another worker refreshes with different content, rewriting the file.
    import time
    time.sleep(0.01)  # ensure a distinct mtime
    monkeypatch.setattr(server.si_client, "list_products", lambda: [
        {"id": "p2", "product_name": "Gadget", "product_code": "G1", "cost": 9}
    ])
    with open(server.CATALOG_CACHE_PATH, "w") as f:
        import json
        json.dump({"fetched_at": "2026-01-02T00:00:00", "products": [
            {"product_id": "p2", "name": "Gadget", "code": "G1", "cost": 9},
        ]}, f)

    products, fetched_at = server.get_catalog()

    assert products == [{"product_id": "p2", "name": "Gadget", "code": "G1", "cost": 9}]
    assert fetched_at == "2026-01-02T00:00:00"
    # Stale cached match for the old catalog must not survive the reload.
    assert server._lookup_cache.get("widget") is None
