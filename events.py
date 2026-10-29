"""Structured event log: one JSON object per line in events.log.

Answers "what did the user do, what did the page show, and what did the
server decide?" for a single GRN attempt — the questions timings.log
(durations) and actions.log (match decisions) can't. Written by the
Streamlit app (layer "ui") and the Flask server (layer "api") into the same
file, joined by `attempt_id`.

Context (session_id / browser_id / attempt_id) is bound per thread with
bind() and merged into every event, so call sites only pass what's new.

Logging must never break the app: every failure in here is swallowed.
"""

import json
import os
import subprocess
import threading
import uuid
from datetime import datetime, timezone

_LOG_PATH = os.environ.get("SI_EVENTS_LOG") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "events.log"
)
_write_lock = threading.Lock()
_ctx = threading.local()

BODY_HEAD_CHARS = 300


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def new_attempt_id():
    return "att-" + uuid.uuid4().hex[:8]


def new_id(prefix):
    return f"{prefix}-" + uuid.uuid4().hex[:6]


def bind(**ctx):
    """Replaces this thread's context. None values are dropped."""
    _ctx.value = {k: v for k, v in ctx.items() if v is not None}


def clear():
    _ctx.value = {}


def current(key=None):
    ctx = getattr(_ctx, "value", {})
    return ctx if key is None else ctx.get(key)


def body_head(text, limit=BODY_HEAD_CHARS):
    """First `limit` characters of a response body, for non-2xx logging."""
    text = text if isinstance(text, str) else str(text)
    return text if len(text) <= limit else text[:limit] + "…"


def app_version():
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, timeout=2,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def log_event(event, layer, level=None, **fields):
    record = {"ts": _now()}
    if level:
        record["level"] = level
    record["layer"] = layer
    record["event"] = event
    record.update(current())
    record.update({k: v for k, v in fields.items() if v is not None})
    try:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _write_lock:
            with open(_LOG_PATH, "a") as f:
                f.write(line + "\n")
    except Exception:
        pass
