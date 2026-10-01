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
    server._catalog_state["checked_disk"] = False

    products, _ = server.get_catalog()

    assert calls["n"] == 1  # no second API call
    assert products[0]["product_id"] == "p1"


def test_get_catalog_checks_disk_only_once_per_process(tmp_path, monkeypatch):
    _no_cache_file(tmp_path, monkeypatch)
    load_calls = {"n": 0}
    orig_load = server._load_catalog_from_file

    def counting_load():
        load_calls["n"] += 1
        return orig_load()

    monkeypatch.setattr(server, "_load_catalog_from_file", counting_load)
    monkeypatch.setattr(server.si_client, "list_products", lambda: [])

    server.get_catalog()
    server.get_catalog()
    server.get_catalog()

    assert load_calls["n"] == 1
