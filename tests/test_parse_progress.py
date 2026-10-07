"""Integration tests for /parse's job-based path (server.py) added so a
slow agent run doesn't block the request: when the fast tiers (exact/cache/
fuzzy match) resolve everything, /parse responds exactly as before (no
job_id). When something needs the LLM agent, /parse now hands off to a
background thread and responds immediately with a job_id to poll via
GET /job/status/<job_id>.

Mirrors tests/test_product_matching.py's convention of monkeypatching
server.query to a no-op async generator (the real Claude Agent SDK's tool
dispatch isn't exercised by any test in this repo — see that file's
tests), which means a line that reaches the agent in these tests never
actually resolves via a real submit_result call. That's enough to verify
the job lifecycle (created -> running -> done/error) and the HTTP
contract; the submit_result-triggered progress increments themselves are
manual-only (same limitation test_product_matching.py already has for
agent-resolved items), verified against timings.log during manual testing.
"""

import sys
import time
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import job_store  # noqa: E402
import server  # noqa: E402


def _catalog(*names_and_codes):
    return [
        {"product_id": f"p{i}", "name": name, "code": code, "cost": 0}
        for i, (name, code) in enumerate(names_and_codes, start=1)
    ]


@pytest.fixture
def jobs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(job_store, "JOBS_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(monkeypatch, jobs_dir):
    monkeypatch.setattr(server, "si_client", object())  # just needs to be non-None
    yield server.app.test_client()


def _set_catalog(monkeypatch, catalog):
    monkeypatch.setattr(server, "_catalog_state", {
        "products": catalog,
        "fetched_at": "2026-01-01T00:00:00",
        "index": server.CatalogIndex(catalog),
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


def test_fully_resolved_parse_has_no_job_id(monkeypatch, client):
    """Every line resolved by the fast tiers -> same response shape as
    before this feature existed, so existing frontend code/tests that only
    know {"items": [...]} keep working unmodified."""
    _set_catalog(monkeypatch, _catalog(("Cap Large Black", "C001")))

    resp = client.post("/parse", json={"text": "Cap large black 5", "mode": "goods_received"})

    assert resp.status_code == 200
    data = resp.get_json()
    assert "job_id" not in data
    assert len(data["items"]) == 1
    assert data["items"][0]["matched_product_id"] == "p1"


def test_unresolved_line_spawns_a_job_that_completes(monkeypatch, client):
    async def fake_query(prompt, options):
        return
        yield  # pragma: no cover - no-op agent, matches test_product_matching.py's convention

    monkeypatch.setattr(server, "query", fake_query)
    _set_catalog(monkeypatch, _catalog(("Widget Blue Deluxe", "B001")))

    resp = client.post("/parse", json={"text": "Totally Unknown Thing 2", "mode": "goods_received"})

    assert resp.status_code == 202
    data = resp.get_json()
    assert "job_id" in data
    assert data["total"] == 1
    assert data["done"] == 0

    job = _wait_for_job(data["job_id"])
    assert job["status"] == "done"
    assert job["items"] == []  # the no-op fake agent never resolves it


def test_mixed_resolved_and_unresolved_reports_correct_total_and_done(monkeypatch, client):
    async def fake_query(prompt, options):
        return
        yield  # pragma: no cover

    monkeypatch.setattr(server, "query", fake_query)
    _set_catalog(monkeypatch, _catalog(("Cap Large Black", "C001")))

    resp = client.post(
        "/parse",
        json={"text": "Cap large black 5\nTotally Unknown Thing 2", "mode": "goods_received"},
    )

    assert resp.status_code == 202
    data = resp.get_json()
    assert data["total"] == 2
    assert data["done"] == 1  # the exact-match line resolved synchronously already

    job = _wait_for_job(data["job_id"])
    assert job["status"] == "done"
    assert job["total"] == 2
    assert len(job["items"]) == 1  # just the exact match; the unknown line never resolves
    assert job["items"][0]["matched_product_id"] == "p1"


def test_agent_exception_marks_job_as_error(monkeypatch, client):
    async def failing_query(prompt, options):
        raise RuntimeError("agent exploded")
        yield  # pragma: no cover

    monkeypatch.setattr(server, "query", failing_query)
    _set_catalog(monkeypatch, _catalog(("Widget Blue Deluxe", "B001")))

    resp = client.post("/parse", json={"text": "Totally Unknown Thing 2", "mode": "goods_received"})
    job_id = resp.get_json()["job_id"]

    job = _wait_for_job(job_id)
    assert job["status"] == "error"
    assert "agent exploded" in job["error"]


def test_job_status_for_unknown_job_is_404(client):
    resp = client.get("/job/status/does-not-exist")
    assert resp.status_code == 404


def test_new_products_mode_always_goes_through_the_job_path(monkeypatch, client):
    """new_products has no catalog to fast-match against, so every call
    needs the agent -- unlike goods_received/product_updates, there's no
    "fully resolved, no job needed" case for this mode."""
    async def fake_query(prompt, options):
        return
        yield  # pragma: no cover

    monkeypatch.setattr(server, "query", fake_query)
    _set_catalog(monkeypatch, _catalog(("Existing Product", "E001")))

    resp = client.post(
        "/parse", json={"text": "add new product: Kickboards Large, price 8.50", "mode": "new_products"},
    )

    assert resp.status_code == 202
    assert "job_id" in resp.get_json()
