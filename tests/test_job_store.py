"""Unit tests for job_store.py's file-backed job progress store.

Monkeypatches job_store.JOBS_DIR to a tmp_path so tests never touch the
real parse_jobs/ directory, and checks the atomic-write guarantee that
makes this safe to poll from a different gunicorn worker process than the
one writing it: a reader never observes a half-written file.
"""

import json
import sys
import threading
import time
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import job_store  # noqa: E402


@pytest.fixture
def jobs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(job_store, "JOBS_DIR", str(tmp_path))
    return tmp_path


def test_read_unknown_job_returns_none(jobs_dir):
    assert job_store.read_job("nope") is None


def test_create_then_read_job(jobs_dir):
    job_store.create_job("j1", total=10, kind="parse", done=3)
    job = job_store.read_job("j1")
    assert job["status"] == "running"
    assert job["total"] == 10
    assert job["done"] == 3
    assert job["kind"] == "parse"
    assert "created_at" in job and "updated_at" in job


def test_update_job_merges_fields(jobs_dir):
    job_store.create_job("j1", total=10, kind="parse")
    job_store.update_job("j1", done=5)
    job_store.update_job("j1", status="done", items=[{"a": 1}])
    job = job_store.read_job("j1")
    assert job["done"] == 5
    assert job["status"] == "done"
    assert job["items"] == [{"a": 1}]
    assert job["total"] == 10  # untouched fields survive the merge


def test_update_job_bumps_updated_at(jobs_dir):
    job_store.create_job("j1", total=10, kind="parse")
    first = job_store.read_job("j1")["updated_at"]
    time.sleep(0.01)
    job_store.update_job("j1", done=1)
    second = job_store.read_job("j1")["updated_at"]
    assert second > first


def test_update_unknown_job_is_a_noop(jobs_dir):
    job_store.update_job("nope", done=1)  # must not raise
    assert job_store.read_job("nope") is None


def test_delete_job_removes_it(jobs_dir):
    job_store.create_job("j1", total=10, kind="parse")
    job_store.delete_job("j1")
    assert job_store.read_job("j1") is None


def test_delete_unknown_job_is_a_noop(jobs_dir):
    job_store.delete_job("nope")  # must not raise


def test_cleanup_stale_jobs_removes_old_ones_only(jobs_dir):
    job_store.create_job("old", total=1, kind="parse")
    job_store.create_job("fresh", total=1, kind="parse")

    old_path = jobs_dir / "old.json"
    old_time = time.time() - 10000
    import os
    os.utime(old_path, (old_time, old_time))

    job_store.cleanup_stale_jobs(max_age_seconds=7200)

    assert job_store.read_job("old") is None
    assert job_store.read_job("fresh") is not None


def test_cleanup_stale_jobs_on_missing_dir_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(job_store, "JOBS_DIR", str(tmp_path / "does_not_exist"))
    job_store.cleanup_stale_jobs()  # must not raise


def test_concurrent_reads_never_see_a_partial_write(jobs_dir):
    """The core cross-worker-safety guarantee: a reader hammering read_job
    while a writer repeatedly update_job()s the same job must only ever
    see fully-formed JSON (valid dict with all expected keys), never a
    JSONDecodeError or a half-written document — this is what the
    temp-file + os.replace() atomic rename buys us.
    """
    job_store.create_job("j1", total=1000, kind="parse")
    stop = threading.Event()
    bad_reads = []

    def writer():
        for i in range(500):
            job_store.update_job("j1", done=i, payload="x" * 500)
        stop.set()

    def reader():
        while not stop.is_set():
            job = job_store.read_job("j1")
            if job is not None and ("done" not in job or "total" not in job):
                bad_reads.append(job)

    w = threading.Thread(target=writer)
    r = threading.Thread(target=reader)
    r.start()
    w.start()
    w.join()
    r.join()

    assert bad_reads == []


def test_job_file_is_valid_json_on_disk(jobs_dir):
    job_store.create_job("j1", total=5, kind="confirm")
    path = jobs_dir / "j1.json"
    assert path.exists()
    with open(path) as f:
        data = json.load(f)
    assert data["total"] == 5
    assert data["kind"] == "confirm"
