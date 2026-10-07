"""File-backed job-progress store for long-running /parse and /confirm work.

Production runs this app under gunicorn with multiple worker processes
(see deploy/sales-intellect-flask.service), each a separate OS process with
its own memory — a module-level dict would NOT be visible across workers,
so a POST that starts a background thread in one worker and a polling GET
that lands on another worker would see different state. Writing job state
to disk (mirroring the existing catalog_cache.json precedent) sidesteps
that: any worker that receives a request can just read the same file.

Each job is one JSON file under JOBS_DIR. Writes go to a uniquely-named
temp file first, then os.replace() onto the real path — atomic on POSIX,
so a concurrent reader always sees either the fully-old or fully-new
file, never a half-written one.

Concurrency note: update_job() does a plain read-modify-write, which is
only safe because exactly one thread ever owns writes to a given job_id
(the single background thread processing that job) — concurrent *readers*
(status polls) are fine thanks to the atomic rename above, but this module
does not support two writers updating the same job_id concurrently.
"""

import json
import os
import time
import uuid

JOBS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parse_jobs")


def _job_path(job_id):
    return os.path.join(JOBS_DIR, f"{job_id}.json")


def _write(job_id, data):
    os.makedirs(JOBS_DIR, exist_ok=True)
    tmp_path = os.path.join(JOBS_DIR, f"{job_id}.json.tmp-{uuid.uuid4().hex}")
    with open(tmp_path, "w") as f:
        json.dump(data, f)
    os.replace(tmp_path, _job_path(job_id))


def create_job(job_id, total, kind, done=0):
    """kind: "parse" or "confirm" — just a label for debugging/logging,
    both kinds share this same store and the same status shape."""
    now = time.time()
    _write(job_id, {
        "kind": kind,
        "status": "running",
        "total": total,
        "done": done,
        "created_at": now,
        "updated_at": now,
    })


def update_job(job_id, **fields):
    """Read-modify-write merge of `fields` into the job's state. See the
    module docstring's concurrency note — only safe from the single thread
    that owns this job_id."""
    job = read_job(job_id)
    if job is None:
        return
    job.update(fields)
    job["updated_at"] = time.time()
    _write(job_id, job)


def read_job(job_id):
    """Returns the job's current state dict, or None if unknown/expired/
    corrupted (a job file caught mid-write by a non-atomic reader elsewhere,
    or removed by cleanup_stale_jobs, reads back as "not found" rather than
    raising)."""
    path = _job_path(job_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def delete_job(job_id):
    path = _job_path(job_id)
    try:
        os.remove(path)
    except OSError:
        pass


def cleanup_stale_jobs(max_age_seconds=7200):
    """Deletes job files untouched for longer than max_age_seconds —
    covers both abandoned jobs (tab closed mid-parse; the background
    thread still finishes server-side but nothing ever polls the result)
    and normal completed jobs nobody ever got around to clearing. Called
    opportunistically at the top of each new /parse or /confirm request
    that's about to create a job, rather than on a separate schedule."""
    if not os.path.isdir(JOBS_DIR):
        return
    now = time.time()
    for name in os.listdir(JOBS_DIR):
        if not name.endswith(".json"):
            continue
        path = os.path.join(JOBS_DIR, name)
        try:
            if now - os.path.getmtime(path) > max_age_seconds:
                os.remove(path)
        except OSError:
            pass
