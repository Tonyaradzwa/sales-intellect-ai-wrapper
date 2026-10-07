"""Integration tests for /confirm's job-based path (server.py).

_submit_grn (goods_received) makes one batch API call — no per-item loop,
so a GRN-only submission stays synchronous with no job_id, exactly as
before this feature existed. stock_update/new_product items ("other_items")
go through a real per-item server-side loop, so when any are present,
/confirm now hands off to a background thread with real progress via the
same job_store mechanism /parse uses, polled via GET /job/status/<job_id>.
"""

import sys
import time
from pathlib import Path
from unittest import mock

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import job_store  # noqa: E402
import server  # noqa: E402


@pytest.fixture
def jobs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(job_store, "JOBS_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_si_client(monkeypatch):
    fake = mock.Mock()
    fake.list_shops.return_value = [{"id": "shop-1", "shop_name": "Branch A"}]
    monkeypatch.setattr(server, "si_client", fake)
    return fake


@pytest.fixture
def client(fake_si_client, jobs_dir):
    return server.app.test_client()


def _set_catalog(monkeypatch, products):
    monkeypatch.setattr(server, "_catalog_state", {
        "products": products,
        "fetched_at": "2026-01-01T00:00:00",
        "index": server.CatalogIndex(products),
        "checked_disk": True,
    })


def _wait_for_job(job_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = job_store.read_job(job_id)
        if job is not None and job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def test_grn_only_submission_has_no_job_id(client, fake_si_client, monkeypatch):
    _set_catalog(monkeypatch, [{"product_id": "p1", "name": "Item A", "code": "A001", "cost": 2}])
    fake_si_client.create_grn.return_value = {"grn_id": "g1"}

    resp = client.post("/confirm", json={
        "shop_id": "shop-1",
        "supplier_id": "sup-1",
        "items": [{
            "action": "goods_received", "raw_text": "Item A x5",
            "matched_product_id": "p1", "quantity": 5, "name": "Item A",
        }],
    })

    assert resp.status_code == 200
    data = resp.get_json()
    assert "job_id" not in data
    assert len(data["results"]) == 1
    assert data["results"][0]["success"] is True
    fake_si_client.create_grn.assert_called_once()


def test_stock_update_only_spawns_job_with_real_per_item_progress(client, fake_si_client, monkeypatch):
    fake_si_client.adjust_inventory.side_effect = lambda shop_id, product_id, qty: {"ok": True}

    resp = client.post("/confirm", json={
        "shop_id": "shop-1",
        "items": [
            {"action": "stock_update", "raw_text": "A x1", "matched_product_id": "p1", "quantity": 1, "name": "A"},
            {"action": "stock_update", "raw_text": "B x2", "matched_product_id": "p2", "quantity": 2, "name": "B"},
            {"action": "stock_update", "raw_text": "C x3", "matched_product_id": "p3", "quantity": 3, "name": "C"},
        ],
    })

    assert resp.status_code == 202
    data = resp.get_json()
    assert data["total"] == 3
    assert data["done"] == 0

    job = _wait_for_job(data["job_id"])
    assert job["status"] == "done"
    assert job["total"] == 3
    assert len(job["results"]) == 3
    assert all(r["success"] for r in job["results"])
    assert fake_si_client.adjust_inventory.call_count == 3


def test_mixed_other_items_and_grn_reports_combined_total_and_progress(client, fake_si_client, monkeypatch):
    _set_catalog(monkeypatch, [{"product_id": "p2", "name": "Item B", "code": "B001", "cost": 1}])
    fake_si_client.adjust_inventory.return_value = {"ok": True}
    fake_si_client.create_grn.return_value = {"grn_id": "g1"}

    resp = client.post("/confirm", json={
        "shop_id": "shop-1",
        "supplier_id": "sup-1",
        "items": [
            {"action": "stock_update", "raw_text": "A x1", "matched_product_id": "p1", "quantity": 1, "name": "A"},
            {"action": "goods_received", "raw_text": "Item B x2", "matched_product_id": "p2", "quantity": 2, "name": "Item B"},
        ],
    })

    assert resp.status_code == 202
    data = resp.get_json()
    assert data["total"] == 2  # 1 other_item + 1 pseudo-step for the GRN batch call

    job = _wait_for_job(data["job_id"])
    assert job["status"] == "done"
    assert job["done"] == 2
    assert len(job["results"]) == 2
    fake_si_client.create_grn.assert_called_once()
    fake_si_client.adjust_inventory.assert_called_once()


def test_failed_item_is_recorded_but_job_still_completes(client, fake_si_client):
    resp = client.post("/confirm", json={
        "shop_id": "shop-1",
        "items": [
            {"action": "stock_update", "raw_text": "No match", "matched_product_id": None, "quantity": 1, "name": ""},
        ],
    })

    job = _wait_for_job(resp.get_json()["job_id"])
    assert job["status"] == "done"
    assert job["results"][0]["success"] is False
    assert "Could not update inventory" in job["results"][0]["error"]


def test_job_backed_confirm_calls_si_client_exactly_once_per_item(client, fake_si_client):
    """Regression check for the lost-serialization risk the plan flagged:
    the background thread must process other_items sequentially, one API
    call per item, not double-call or skip any."""
    fake_si_client.adjust_inventory.return_value = {"ok": True}

    resp = client.post("/confirm", json={
        "shop_id": "shop-1",
        "items": [
            {"action": "stock_update", "raw_text": f"Item {i}", "matched_product_id": f"p{i}",
             "quantity": i, "name": f"Item {i}"}
            for i in range(5)
        ],
    })

    job = _wait_for_job(resp.get_json()["job_id"])
    assert job["status"] == "done"
    assert fake_si_client.adjust_inventory.call_count == 5
    assert len(job["results"]) == 5
