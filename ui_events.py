"""Streamlit-side glue for events.py: session / browser / attempt IDs kept in
st.session_state, and thin wrappers that log "ui"-layer events with that
context attached.

- session_id: one per Streamlit session. A reload, websocket reconnect, new
  tab or server restart all mint a new one.
- browser_id: stored in a cookie, so it survives all of those. Same
  browser_id + new session_id = same browser, not another person/device.
- attempt_id: one per paste-to-GRN attempt. Sent to Flask as X-Request-ID so
  the server's events and gunicorn's access log carry the same ID.
"""

import hashlib
import time

import streamlit as st
import streamlit.components.v1 as components

import events

BROWSER_COOKIE = "si_browser_id"
_COOKIE_MAX_AGE_S = 60 * 60 * 24 * 365

_SESSION_KEY = "_ui_session_id"
_BROWSER_KEY = "_ui_browser_id"
_ATTEMPT_KEY = "_ui_attempt_id"
_ATTEMPT_SHA_KEY = "_ui_attempt_sha"
_ATTEMPT_DONE_KEY = "_ui_attempt_done"
_CLICKED_KEY = "_ui_clicked_attempts"


def _read_cookie_and_ua():
    # st.context is unavailable outside a real session (and in some test
    # harnesses) — a missing cookie just means a fresh browser_id.
    try:
        cookie = st.context.cookies.get(BROWSER_COOKIE)
    except Exception:
        cookie = None
    try:
        ua = st.context.headers.get("User-Agent")
    except Exception:
        ua = None
    return cookie, ua


def _set_browser_cookie(browser_id):
    # Streamlit has no cookie-writing API; a zero-height component runs JS
    # in a same-origin iframe, which can set the parent page's cookie.
    try:
        components.html(
            f"<script>window.parent.document.cookie = "
            f"'{BROWSER_COOKIE}={browser_id}; max-age={_COOKIE_MAX_AGE_S}; "
            f"path=/; SameSite=Lax';</script>",
            height=0,
        )
    except Exception:
        pass


def sync_context():
    """Binds this run's thread to the session's IDs so every events.log_event
    and timing.log line carries them. Cheap; call at the top of every run."""
    ss = st.session_state
    events.bind(
        session_id=ss.get(_SESSION_KEY),
        browser_id=ss.get(_BROWSER_KEY),
        attempt_id=ss.get(_ATTEMPT_KEY),
    )


def init_session():
    """Once per Streamlit session: assign IDs and log session_start. Safe to
    call on every run."""
    ss = st.session_state
    if _SESSION_KEY not in ss:
        cookie, ua = _read_cookie_and_ua()
        first_seen = not cookie
        browser_id = cookie or events.new_id("b")
        if first_seen:
            _set_browser_cookie(browser_id)
        ss[_SESSION_KEY] = events.new_id("s")
        ss[_BROWSER_KEY] = browser_id
        sync_context()
        log("session_start", ua=(ua or "")[:200] or None, first_seen=first_seen)
    sync_context()


def log(event, level=None, **fields):
    events.log_event(event, "ui", level=level, **fields)


def current_attempt_id():
    return st.session_state.get(_ATTEMPT_KEY)


def _text_sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:6]


def begin_attempt_for_text(text):
    """Returns the attempt_id for this pasted text, starting a new attempt if
    the text changed or the previous attempt already ended. Called on every
    run that has paste text, so a re-render of the same text keeps its ID."""
    ss = st.session_state
    sha = _text_sha(text)
    if ss.get(_ATTEMPT_SHA_KEY) != sha or ss.get(_ATTEMPT_DONE_KEY) or not ss.get(_ATTEMPT_KEY):
        ss[_ATTEMPT_KEY] = events.new_attempt_id()
        ss[_ATTEMPT_SHA_KEY] = sha
        ss[_ATTEMPT_DONE_KEY] = False
        sync_context()
        log(
            "attempt_started", text_sha=sha, text_chars=len(text),
            text_lines=len(text.splitlines()),
        )
    return ss[_ATTEMPT_KEY]


def end_attempt(outcome):
    """Marks the current attempt finished (next paste starts a new one)."""
    ss = st.session_state
    if ss.get(_ATTEMPT_KEY) and not ss.get(_ATTEMPT_DONE_KEY):
        log("attempt_ended", outcome=outcome)
        ss[_ATTEMPT_DONE_KEY] = True


def request_headers():
    """Headers for calls from Streamlit to Flask: the attempt_id as
    X-Request-ID, so server logs and the gunicorn access log carry it."""
    attempt_id = current_attempt_id()
    return {"X-Request-ID": attempt_id} if attempt_id else {}


def log_submit_clicked(**fields):
    ss = st.session_state
    attempt_id = ss.get(_ATTEMPT_KEY)
    ss.setdefault(_CLICKED_KEY, set())
    if attempt_id:
        ss[_CLICKED_KEY].add(attempt_id)
    log("submit_clicked", source="button_click", **fields)


# A busy flag older than this was almost certainly left behind by an earlier
# run, not set by the click that started this one. Logging only for now — see
# the observability plan's change 6b for actually expiring it.
BUSY_STALE_AFTER_S = 60


def log_submit_resumed(busy_age_s=None, prior_attempt_id=None):
    """Called on a run that continues a submit because the busy flag was
    already set. Normal after a click (the flag is the second half of one
    click); suspicious if no submit_clicked was logged for this attempt."""
    ss = st.session_state
    attempt_id = ss.get(_ATTEMPT_KEY)
    if busy_age_s is not None and busy_age_s > BUSY_STALE_AFTER_S:
        log(
            "busy_stale", level="WARN", label="Submit GRN",
            busy_age_s=round(busy_age_s), prior_attempt_id=prior_attempt_id,
            action="logged_only",
        )
    clicked = attempt_id in ss.get(_CLICKED_KEY, set())
    log(
        "submit_resumed", level=None if clicked else "WARN", source="busy_resume",
        note=None if clicked else "no submit_clicked with this attempt_id",
    )


def monotonic_ms(since):
    return round((time.time() - since) * 1000)
