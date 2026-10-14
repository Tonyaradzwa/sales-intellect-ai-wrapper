"""Per-product match-decision log: catalog/cache/fuzzy/agent hit or miss.

Appends one line per resolution decision to actions.log at the repo root —
kept separate from timings.log (duration/performance trace) so each file
stays focused on one question: timings.log answers "how long did this take,"
actions.log answers "what got decided, for which product, and why." Written
only by server.py, from inside the three fast-tier matcher functions, the
LFU-cache write path, and the LLM agent's submit_result tool handlers.

Temporary diagnostic tool, not meant to ship long-term.
"""

import os
import threading
from datetime import datetime

_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "actions.log")
_write_lock = threading.Lock()


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _fmt_fields(fields):
    return "  ".join(f"{k}={v}" for k, v in fields.items() if v is not None)


def log(label, **fields):
    meta = _fmt_fields(fields)
    suffix = "  |  " + meta if meta else ""
    line = f"[{_now()}] {label}{suffix}"
    with _write_lock:
        with open(_LOG_PATH, "a") as f:
            f.write(line + "\n")
