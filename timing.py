"""Local latency instrumentation for diagnosing GRN parse/submit slowness.

Appends a chronological, human-readable trace to timings.log at the repo
root, shared by app.py (Streamlit), server.py (Flask) and client.py (the
Sales Intellect HTTP client) — both processes write to the same file, so a
single "Read items" or "Confirm and Submit" click produces one interleaved,
timestamped trace of everything that happened underneath it.

Temporary diagnostic tool, not meant to ship long-term.
"""

import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime

_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "timings.log")
_write_lock = threading.Lock()
_depth = threading.local()


def _now():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _current_depth():
    return getattr(_depth, "value", 0)


def _write(text):
    with _write_lock:
        with open(_LOG_PATH, "a") as f:
            f.write(text + "\n")


def new_run_id():
    return uuid.uuid4().hex[:8]


def _fmt_fields(fields):
    return "  ".join(f"{k}={v}" for k, v in fields.items() if v is not None)


def log(label, dur=None, **fields):
    """A single chronological line at the current nesting depth."""
    indent = "  " * _current_depth()
    parts = []
    if dur is not None:
        parts.append(f"dur={dur:.3f}s")
    meta = _fmt_fields(fields)
    if meta:
        parts.append(meta)
    suffix = "  |  " + "  ".join(parts) if parts else ""
    _write(f"{indent}[{_now()}] {label}{suffix}")


@contextmanager
def timed(label, **fields):
    """Wraps a block, logging one line with its duration when it exits."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        dur = time.perf_counter() - t0
        log(label, dur=dur, **fields)


@contextmanager
def section(title, **fields):
    """A banner header marking the start of a named process/phase (e.g.
    "PARSE REQUEST", "SUBMITTING GRN"). Everything logged inside it — from
    this or any nested section/timed call, in this thread — indents under
    it, and its own total duration is logged when it exits.
    """
    depth = _current_depth()
    indent = "  " * depth
    meta = _fmt_fields(fields)
    bar = "=" * max(10, 70 - len(indent))
    _write(f"{indent}{bar}")
    _write(f"{indent}[{_now()}] {title}" + (f"  ({meta})" if meta else ""))
    _write(f"{indent}{bar}")
    _depth.value = depth + 1
    t0 = time.perf_counter()
    try:
        yield
    finally:
        dur = time.perf_counter() - t0
        _depth.value = depth
        _write(f"{indent}[{_now()}] << {title} DONE -- dur={dur:.3f}s")
        _write("")
