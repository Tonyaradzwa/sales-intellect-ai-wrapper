"""File-backed record of the last GRN submitted for each shop, used to warn
when a new submit is identical to it (the same delivery sent twice).

Only the previous GRN's payload fingerprint is kept — one entry per shop,
replaced on every successful submit — so there is nothing to look up in the
ERP afterwards. File-backed (like job_store.py) because gunicorn runs several
worker processes, and the warning must work whichever one handled the last
submit. The read-modify-write is guarded by an flock for the same reason.
"""

import fcntl
import hashlib
import json
import os
import time
import uuid

STORE_PATH = os.environ.get("SI_LAST_GRN_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "last_grn.json"
)


def fingerprint(shop_id, grn_date, lines):
    """lines: [(product_id, qty)]. Order of lines doesn't matter."""
    canon = json.dumps(
        {"shop": shop_id, "date": grn_date, "lines": sorted((str(p), int(q)) for p, q in lines)},
        sort_keys=True,
    )
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def _read():
    try:
        with open(STORE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def get(shop_id):
    return _read().get(shop_id)


def put(shop_id, fp, line_count, attempt_id=None):
    """Makes this GRN the shop's "previous" one."""
    lock_path = STORE_PATH + ".lock"
    os.makedirs(os.path.dirname(STORE_PATH) or ".", exist_ok=True)
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read()
        data[shop_id] = {
            "fingerprint": fp, "lines": line_count,
            "submitted_at": time.time(), "attempt_id": attempt_id,
        }
        tmp = f"{STORE_PATH}.tmp-{uuid.uuid4().hex}"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, STORE_PATH)


def age_text(seconds):
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds} s ago"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} min ago"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} days ago"
