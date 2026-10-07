"""Shared loading-state UI helpers for app.py: a full-page blocking
overlay, hiding Streamlit's built-in status widget, and a two-rerun "busy
flag" pattern for disabling a trigger button while its action runs.

One small module, same role as timing.py — imported only by app.py.

Dark mode note: Streamlit (confirmed at the 1.64.0 pinned in
requirements.txt) does not expose its active theme as CSS custom
properties (no --background-color/--text-color/etc. anywhere in its
shipped CSS) — theming is internal to its own emotion-styled React
components, not something plain injected CSS can read. So, like the
existing _render_production_banner()/_MATCH_STATUS_STYLE in app.py, this
module uses hardcoded light/dark palettes switched via a plain
`@media (prefers-color-scheme: dark)` query (the OS/browser signal) —
this covers the overwhelming majority of real usage; the one edge case it
doesn't catch is a staff member manually overriding Streamlit's own
light/dark toggle (in its settings menu) against their OS preference, in
which case this overlay's colors may not match the rest of the page.
"""

from contextlib import contextmanager

import streamlit as st

# Above _render_production_banner's fixed banner (z-index: 1000000, app.py),
# so the overlay still shows on top of it in a production deployment.
_OVERLAY_Z_INDEX = 2000000

_GLOBAL_CSS = f"""
<style>
[data-testid="stStatusWidget"] {{ display: none; }}

.loading-ui-scrim {{
    position: fixed;
    inset: 0;
    z-index: {_OVERLAY_Z_INDEX};
    background: rgba(0, 0, 0, 0.55);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 1rem;
}}

.loading-ui-card {{
    background: #ffffff;
    color: #262730;
    border: 1px solid rgba(49, 51, 63, 0.15);
    border-radius: 12px;
    padding: clamp(1rem, 4vw, 1.75rem) clamp(1.25rem, 5vw, 2.25rem);
    max-width: min(90vw, 360px);
    width: 100%;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.9rem;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.35);
    text-align: center;
}}

.loading-ui-spinner {{
    width: clamp(2rem, 8vw, 2.75rem);
    height: clamp(2rem, 8vw, 2.75rem);
    border-radius: 50%;
    border: 3px solid rgba(49, 51, 63, 0.15);
    border-top-color: #ff4b4b;
    animation: loading-ui-spin 0.9s linear infinite;
}}

@keyframes loading-ui-spin {{
    to {{ transform: rotate(360deg); }}
}}

.loading-ui-message {{
    font-size: clamp(0.9rem, 3vw, 1rem);
    line-height: 1.4;
}}

/* render_progress(): a custom bar (st.progress has no indeterminate/pulse
   mode) with a shimmer sweeping across the filled portion continuously,
   so a long stretch with no real progress update (e.g. one slow
   agent-resolved line between polls) still reads as "working", not stuck. */
.loading-ui-progress-track {{
    width: 100%;
    height: 0.6rem;
    background: rgba(49, 51, 63, 0.15);
    border-radius: 999px;
    overflow: hidden;
}}

.loading-ui-progress-fill {{
    height: 100%;
    min-width: 0.6rem;
    background: #ff4b4b;
    border-radius: 999px;
    position: relative;
    overflow: hidden;
    transition: width 0.4s ease;
}}

.loading-ui-progress-fill::after {{
    content: "";
    position: absolute;
    inset: 0;
    background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.45), transparent);
    animation: loading-ui-shimmer 1.4s ease-in-out infinite;
}}

@keyframes loading-ui-shimmer {{
    0% {{ transform: translateX(-100%); }}
    100% {{ transform: translateX(100%); }}
}}

.loading-ui-progress-text {{
    font-size: 0.85rem;
    margin-top: 0.4rem;
}}

@media (prefers-color-scheme: dark) {{
    .loading-ui-scrim {{ background: rgba(0, 0, 0, 0.7); }}
    .loading-ui-card {{
        background: #0e1117;
        color: #fafafa;
        border-color: rgba(250, 250, 250, 0.15);
    }}
    .loading-ui-spinner {{
        border-color: rgba(250, 250, 250, 0.15);
        border-top-color: #ff4b4b;
    }}
    .loading-ui-progress-track {{ background: rgba(250, 250, 250, 0.15); }}
}}
</style>
"""


def inject_global_css():
    """Call once, right after st.set_page_config(). Hides Streamlit's
    built-in top-right running indicator (replaced by the indicators this
    module and app.py provide — st.status, st.progress, overlay()) and
    defines the overlay's styling."""
    st.markdown(_GLOBAL_CSS, unsafe_allow_html=True)


@contextmanager
def overlay(message):
    """Full-page, blocking loading overlay: a dark scrim covering the
    whole viewport with a centered spinner and message underneath. Use
    only where the user genuinely shouldn't interact with anything else —
    e.g. the initial catalog load, or writing confirmed stock updates.

    Always clears itself in a finally block, even if the wrapped code
    raises, so it can never be left stuck on screen.
    """
    placeholder = st.empty()
    placeholder.markdown(
        f"""
        <div class="loading-ui-scrim">
            <div class="loading-ui-card">
                <div class="loading-ui-spinner"></div>
                <div class="loading-ui-message">{message}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    try:
        yield
    finally:
        placeholder.empty()


def render_progress(placeholder, fraction, text):
    """Renders a determinate progress bar into `placeholder` (an st.empty())
    with a shimmer continuously sweeping across the filled portion — unlike
    st.progress, which only ever looks different when `fraction` itself
    changes. Used for polling loops where real updates can be infrequent
    (one slow remote step between polls): the shimmer keeps reading as
    "still working" through those gaps instead of looking frozen. Call
    again with the same placeholder to update in place.
    """
    fraction = max(0.0, min(1.0, fraction))
    placeholder.markdown(
        f"""
        <div class="loading-ui-progress-track">
            <div class="loading-ui-progress-fill" style="width: {fraction * 100:.1f}%;"></div>
        </div>
        <div class="loading-ui-progress-text">{text}</div>
        """,
        unsafe_allow_html=True,
    )


# --- Two-rerun busy-flag pattern -------------------------------------------
#
# Streamlit re-runs the whole script on every interaction and can't update a
# widget "mid-run" after it's already been drawn — so disabling a button
# while its own action is in flight takes two runs: the click sets a busy
# flag and immediately st.rerun()s; the next run sees the flag, renders the
# button disabled=True, and does the actual work; then clears the flag and
# reruns once more to show the result. These three helpers implement that
# once so it isn't hand-rolled differently at every call site.

def is_busy(key):
    return bool(st.session_state.get(f"_busy__{key}"))


def start_busy(key, **payload):
    """Marks `key` as busy and stashes `payload` in session_state for the
    next run to read back via busy_payload(). Caller must st.rerun() right
    after calling this."""
    st.session_state[f"_busy__{key}"] = True
    for k, v in payload.items():
        st.session_state[f"_busy_payload__{key}__{k}"] = v


def busy_payload(key, payload_key):
    return st.session_state.get(f"_busy_payload__{key}__{payload_key}")


def clear_busy(key, *payload_keys):
    st.session_state[f"_busy__{key}"] = False
    for k in payload_keys:
        st.session_state.pop(f"_busy_payload__{key}__{k}", None)
