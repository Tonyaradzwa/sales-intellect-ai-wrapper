"""Streamlit UI: paste-and-parse into Sales Intellect updates.

Run with:
    export SI_API_TOKEN=your_token_here
    export ANTHROPIC_API_KEY=your_anthropic_key
    streamlit run app.py
(or use ./run.sh to start this together with the backend)
"""

import os
import time
import uuid
from datetime import datetime

import requests
import streamlit as st

import loading_ui
import timing
from client import SalesIntellectClient

BACKEND_URL = os.environ.get("SI_BACKEND_URL", "http://127.0.0.1:5050")


def _env_flag(name, default="false"):
    return os.environ.get(name, default).strip().lower() == "true"


# GRN always uses the "N/A" supplier and Cash payment in practice — these
# default to on so the dropdown/caption for a choice nobody actually makes
# isn't on screen. Set to "false" (per deployment, via env var) the day a
# real alternative is needed and the picker should come back.
DEFAULT_SUPPLIER_TO_NA = _env_flag("SI_GRN_DEFAULT_SUPPLIER_TO_NA", default="true")
# Payment method has no selector at all right now (server.py hardcodes
# _GRN_CASH_PAYMENT_METHOD_ID unconditionally) — this flag doesn't gate
# anything yet, it just names the toggle point for when a payment method
# picker is actually built.
DEFAULT_PAYMENT_TO_CASH = _env_flag("SI_GRN_DEFAULT_PAYMENT_TO_CASH", default="true")
# Off by default — with DEFAULT_SUPPLIER_TO_NA also on by default, the
# Supplier dropdown is hidden and never changes, so a button to refresh
# supplier data has nothing to serve. Set to "true" if DEFAULT_SUPPLIER_TO_NA
# is ever turned off and the dropdown (and a reason to refresh it) is back.
SHOW_REFRESH_SUPPLIERS = _env_flag("SI_SHOW_REFRESH_SUPPLIERS", default="false")

# Prod and preprod are two separate deployments of this same codebase, each
# with its own .env (and crucially its own SI_API_TOKEN — see .env.example).
# APP_ENV just controls the visual "you are in production" tell below; it
# doesn't switch which API key is used — that's whatever SI_API_TOKEN is set
# to on this instance.
APP_ENV = os.environ.get("APP_ENV", "preprod").strip().lower()
IS_PRODUCTION = APP_ENV == "production"
PREPROD_URL = os.environ.get("PREPROD_URL", "")

# Per-tab feature flags, and the order tabs render in. Toggle via env vars
# (e.g. SI_TAB_PRODUCT_UPDATES=true) without touching this file.
TAB_CONFIG = [
    {
        "label": "Goods Received",
        "enabled": os.environ.get("SI_TAB_GOODS_RECEIVED", "true").lower() == "true",
        "render_kwargs": dict(
            key_prefix="grn",
            mode="goods_received",
            # Implementation notes: all items in one submission are recorded
            # as a single GRN, and payment method is fixed to Cash — the
            # only one currently supported, see _GRN_CASH_PAYMENT_METHOD_ID
            # in server.py.
            show_shop_selector=True,
            show_supplier_selector=True,
        ),
    },
    {
        "label": "Product Updates",
        "enabled": os.environ.get("SI_TAB_PRODUCT_UPDATES", "false").lower() == "true",
        "render_kwargs": dict(
            key_prefix="updates",
            mode="product_updates",
            subheader="Product Updates",
            caption=(
                "Paste a raw WhatsApp message describing stock received for existing "
                "products. Every item here is a stock update against an existing "
                "product — use Bulk Product Upload for brand new products. Edit or "
                "delete a row in the table below before confirming; nothing is "
                "written to Sales Intellect until then."
            ),
            show_shop_selector=True,
        ),
    },
    {
        "label": "Bulk Product Upload",
        "enabled": os.environ.get("SI_TAB_BULK_UPLOAD", "false").lower() == "true",
        "render_kwargs": dict(
            key_prefix="bulk",
            mode="new_products",
            subheader="Bulk Product Upload",
            caption=(
                "Paste a raw WhatsApp message listing brand new products to add "
                "(e.g. \"add new product: Kickboards Large, price 8.50\"). No shop "
                "needed — these are created as new products, not stock updates. Edit "
                "or delete a row in the table below before confirming; nothing is "
                "written to Sales Intellect until then."
            ),
            show_shop_selector=False,
        ),
    },
]


def get_client():
    if "client" not in st.session_state:
        st.session_state.client = SalesIntellectClient()
    return st.session_state.client


def _shop_options(shops):
    if isinstance(shops, dict):
        for value in shops.values():
            if isinstance(value, list):
                shops = value
                break
    options = {}
    for s in shops:
        shop_id = s.get("id") or s.get("shop_id")
        name = s.get("shop_name") or s.get("name") or shop_id
        options[name] = shop_id
    return options


def _supplier_options(suppliers):
    options = {}
    for s in suppliers:
        supplier_id = s.get("id")
        label = s.get("supplier_name") or s.get("supplier_id") or supplier_id
        # "N/A" is the catch-all supplier for deliveries with no tracked
        # supplier account — showing its internal code (e.g. "N/A (COM1)")
        # is just noise for staff.
        if label != "N/A" and s.get("supplier_id") and s.get("supplier_id") != label:
            label = f"{label} ({s['supplier_id']})"
        options[label] = supplier_id
    return options


def _product_lookup(products):
    """id -> {"name", "code"}, from the cached catalog (GET /catalog's
    {product_id, name, code, cost} shape — see server.py's get_catalog())."""
    lookup = {}
    for p in products:
        lookup[p.get("product_id")] = {"name": p.get("name"), "code": p.get("code")}
    return lookup


def _session_cached(session_key, fetch_fn, spinner_message):
    """Fetches via fetch_fn() once per browser session and caches the
    result in st.session_state — no TTL. Without this, shops/suppliers/
    catalog data gets refetched on every single Streamlit rerun (the whole
    script re-runs on every interaction), which was costing ~3s on every
    click before this existed. Cleared only by an explicit "Refresh ..."
    action elsewhere (see the "Refresh Catalog"/"Refresh Suppliers"
    buttons below). On failure, nothing is cached — a transient error
    doesn't lock in a missing value for the rest of the session.
    """
    if session_key in st.session_state:
        return st.session_state[session_key]
    with st.spinner(spinner_message):
        value = fetch_fn()
    st.session_state[session_key] = value
    return value


def _fetch_catalog():
    """Fetches the shared, backend-cached product catalog. Normally served
    from the backend's in-memory/file cache (fast), but when there's no
    cache file yet (e.g. first run), this triggers the same full live,
    paginated fetch as "Refresh Catalog" — hence the generous timeout,
    matching POST /catalog/refresh below."""
    with timing.timed("http_get_catalog"):
        resp = requests.get(f"{BACKEND_URL}/catalog", timeout=120)
    resp.raise_for_status()
    return resp.json()


def _format_fetched_at(fetched_at):
    """"2026-10-05T11:58:21" -> "5 October 2026 at 11:58". Falls back to
    the raw value if it's not in the expected ISO shape (server.py's
    datetime.now().isoformat(timespec="seconds"))."""
    if not fetched_at:
        return fetched_at
    try:
        dt = datetime.fromisoformat(fetched_at)
    except ValueError:
        return fetched_at
    return f"{dt.day} {dt.strftime('%B %Y at %H:%M')}"


def _with_row_ids(items):
    for item in items:
        item["_rid"] = uuid.uuid4().hex
    return items


# Match-status -> (background tint, left-border accent) for the color-coded
# name field in compact mode. Transparent backgrounds so text stays readable
# in both light and dark themes. Only unmatched gets highlighted — a
# "non-exact but matched" (fuzzy) state used to get a yellow highlight too,
# but that turned out not to be useful in practice: once staff pick a
# product for a flagged row, it's resolved and doesn't need to keep
# standing out, so fuzzy rows render with no special styling at all.
_MATCH_STATUS_STYLE = {
    "exact": ("rgba(34, 197, 94, 0.16)", "#22c55e"),
    "unmatched": ("rgba(239, 68, 68, 0.16)", "#ef4444"),
}


def _render_match_rows(
    items, key_prefix, action, qty_label, product_lookup, compact=False,
    hide_statuses=frozenset(), hidden_caption=None,
):
    """Shared review-table rendering for both product_updates (stock_update)
    and goods_received: a dropdown to confirm/override the matched product,
    a quantity field, and a delete button.

    compact: GRN's simplified layout — no separate Code column (the product
    name option already shows "Name (CODE)") and no separate Flag column;
    instead the name field itself is color-coded (green = exact match,
    red = unmatched; a fuzzy/non-exact match gets no highlight — once
    staff have picked a product it's resolved, nothing more to flag) via
    _MATCH_STATUS_STYLE.

    hide_statuses (compact only): statuses ("exact", "fuzzy", "unmatched")
    to skip rendering entirely — those items are passed through unchanged,
    so only the statuses left out need a look. Used for GRN's trust mode
    ({"exact"}) and SKIP REVIEW's flagged-only correction view
    ({"exact", "fuzzy"}).

    hidden_caption: if given and at least one row was hidden, shown as
    hidden_caption.format(n=hidden_count) below the table.
    """
    all_options = sorted(
        ((pid, f"{info['name']} ({info['code']})") for pid, info in product_lookup.items()),
        key=lambda pair: pair[1],
    )

    if compact:
        widths = [5.5, 1, 0.5]
        headers = ["Product", qty_label, ""]
    else:
        widths = [4.5, 1.1, 1, 2, 0.5]
        headers = ["Product / Match", "Code", qty_label, "Flag", ""]

    for col, header in zip(st.columns(widths), headers):
        col.markdown(f"**{header}**")

    edited_items = []
    deleted_rid = None
    row_styles = []  # [(css_key, status)] collected for the single style block below
    hidden_count = 0

    for item in items:
        rid = item["_rid"]

        candidates = item.get("candidates") or []
        matched_id = item.get("matched_product_id")

        option_ids = [c["product_id"] for c in candidates]
        option_labels = {
            c["product_id"]: f"{c['name']} ({product_lookup.get(c['product_id'], {}).get('code', '')})"
            for c in candidates
        }
        if matched_id and matched_id not in option_ids:
            option_ids.insert(0, matched_id)
            option_labels[matched_id] = (
                f"{item.get('name', matched_id)} "
                f"({product_lookup.get(matched_id, {}).get('code', '')})"
            )
        if not option_ids:
            option_ids = [pid for pid, _ in all_options]
            option_labels = dict(all_options)

        ids_ordered = [None] + option_ids
        labels_ordered = ["— select a product —"] + [option_labels[pid] for pid in option_ids]
        default_index = ids_ordered.index(matched_id) if matched_id in ids_ordered else 0

        # Backend's verdict before any user interaction — used to decide
        # whether this row can be skipped untouched (hide_statuses).
        backend_exact = bool(item.get("exact_match")) and matched_id is not None
        backend_status = "exact" if backend_exact else ("unmatched" if matched_id is None else "fuzzy")

        if compact and backend_status in hide_statuses:
            # Trusted as-is — still included in the submitted payload, just
            # not rendered, so there's nothing to review.
            hidden_count += 1
            edited_items.append({
                "_rid": rid,
                "raw_text": item.get("raw_text", ""),
                "action": action,
                "matched_product_id": matched_id,
                "quantity": int(item.get("quantity") or 0),
                "name": product_lookup.get(matched_id, {}).get("name") or item.get("raw_text", ""),
                "code": None,
                "fields": {},
                "candidates": candidates,
                "exact_match": backend_exact,
            })
            continue

        cols = st.columns(widths)

        if compact:
            name_cell_key = f"{key_prefix}_namecell_{rid}"
            name_cell = cols[0].container(key=name_cell_key)
            chosen_label = name_cell.selectbox(
                "Matched product", labels_ordered, index=default_index,
                key=f"{key_prefix}_match_{rid}", label_visibility="collapsed",
            )
        else:
            chosen_label = cols[0].selectbox(
                "Matched product", labels_ordered, index=default_index,
                key=f"{key_prefix}_match_{rid}", label_visibility="collapsed",
            )
            code = product_lookup.get(matched_id, {}).get("code", "")
            cols[1].text_input(
                "Code", value=code, key=f"{key_prefix}_code_display_{rid}",
                label_visibility="collapsed", disabled=True,
            )

        chosen_id = ids_ordered[labels_ordered.index(chosen_label)]
        is_exact = bool(item.get("exact_match")) and chosen_id == matched_id
        status = "exact" if is_exact else ("unmatched" if chosen_id is None else "fuzzy")

        if compact:
            if status == "unmatched":
                name_cell.caption(f"⚠️ Not found: “{item.get('raw_text', '')}”")
            if status in _MATCH_STATUS_STYLE:
                row_styles.append((name_cell_key, status))

        qty_col = cols[1] if compact else cols[2]
        qty = qty_col.number_input(
            qty_label, value=int(item.get("quantity") or 0),
            step=1, key=f"{key_prefix}_qty_{rid}", label_visibility="collapsed",
        )

        if not compact:
            if chosen_id is None:
                cols[3].caption(f"⚠️ Not found: “{item.get('raw_text', '')}”")
            elif is_exact:
                cols[3].caption("✓ Exact match")

        del_col = cols[2] if compact else cols[4]
        if del_col.button("\U0001F5D1", key=f"{key_prefix}_del_{rid}"):
            deleted_rid = rid

        edited_items.append({
            "_rid": rid,
            "raw_text": item.get("raw_text", ""),
            "action": action,
            "matched_product_id": chosen_id,
            "quantity": qty,
            "name": product_lookup.get(chosen_id, {}).get("name") or item.get("raw_text", ""),
            "code": None,
            "fields": {},
            "candidates": candidates,
            "exact_match": is_exact,
        })

    if row_styles:
        rules = "\n".join(
            f'.st-key-{css_key} {{ background-color: {_MATCH_STATUS_STYLE[status][0]}; '
            f'border-left: 4px solid {_MATCH_STATUS_STYLE[status][1]}; '
            f"border-radius: 6px; padding: 6px 10px; }}"
            for css_key, status in row_styles
        )
        st.markdown(f"<style>{rules}</style>", unsafe_allow_html=True)

    if compact and hidden_caption and hidden_count:
        st.caption(hidden_caption.format(n=hidden_count))

    return edited_items, deleted_rid


def _submit_items(
    key_prefix, items_key, status_key, items,
    selected_shop_id, selected_supplier_id, show_shop_selector, show_supplier_selector,
    label="item",
):
    """POSTs /confirm for a batch of (already-flag-free) items and updates
    status_key/confirm_results accordingly. Shared by every Confirm/Submit
    button across all tabs and flows — GRN's SKIP REVIEW (both submission
    paths: immediately after a clean parse, and once a flagged batch has
    been fixed up) and the classic read-then-review flow's Confirm and
    Submit — so they can't drift apart again.

    Response shapes from POST /confirm (see server.py):
    - {"results": [...]}: a single batch API call (GRN's goods_received)
      already finished by the time this response came back — the overlay
      the caller wraps this call in covers the whole wait, no further
      polling needed.
    - {"job_id": ..., "total": ...}: a real per-item server-side loop
      (stock_update/new_product) is running in the background — poll
      GET /job/status/<job_id> and drive a real progress bar with it.

    label: what to call these items in the status/toast message, e.g.
    "GRN" -> "Submitted GRN with 12 item(s)."
    """
    if show_shop_selector and not selected_shop_id:
        st.session_state[status_key] = "Select a shop first."
        return
    if show_supplier_selector and not selected_supplier_id:
        st.session_state[status_key] = "Select a supplier first."
        return

    payload_items = [{k: v for k, v in it.items() if k != "_rid"} for it in items]
    run_id = timing.new_run_id()
    with timing.section(
        "FRONTEND: CONFIRM AND SUBMIT", run_id=run_id, items=len(payload_items),
    ):
        try:
            with loading_ui.overlay(f"Saving {len(payload_items)} item(s) to Sales Intellect…"):
                with timing.timed("http_post_confirm"):
                    resp = requests.post(
                        f"{BACKEND_URL}/confirm",
                        json={
                            "shop_id": selected_shop_id,
                            "supplier_id": selected_supplier_id,
                            "items": payload_items,
                        },
                        timeout=30,
                    )
        except Exception as e:
            st.session_state[status_key] = (
                f"Could not reach confirm backend at {BACKEND_URL}: {e}. "
                "Check your connection and try again."
            )
            timing.log("confirm_exception", error=str(e))
            return

        if not resp.ok:
            error = resp.json().get("error", resp.text)
            st.session_state[status_key] = f"Submit failed: {error}. Check the items and try again."
            timing.log("confirm_failed", error=error)
            return

        data = resp.json()

        if "job_id" not in data:
            # Single batch call (GRN) — already fully resolved.
            results = data["results"]
            timing.log("confirm_result", results=len(results))
        else:
            job_id = data["job_id"]
            total = data.get("total", 0)
            progress_box = st.empty()
            loading_ui.render_progress(progress_box, 0, f"Updating 0 of {total}…")
            deadline = time.time() + 300  # matches the old client-side timeout
            job = None
            while time.time() < deadline:
                try:
                    poll_resp = requests.get(f"{BACKEND_URL}/job/status/{job_id}", timeout=10)
                    poll_resp.raise_for_status()
                    job = poll_resp.json()
                except Exception as e:
                    timing.log("confirm_poll_exception", error=str(e))
                    time.sleep(1.5)
                    continue

                done = job.get("done", 0)
                loading_ui.render_progress(
                    progress_box, done / total if total else 0, f"Updating {done} of {total}…",
                )
                if job.get("status") in ("done", "error"):
                    break
                time.sleep(1.5)
            progress_box.empty()

            if job is None or job.get("status") not in ("done", "error"):
                st.session_state[status_key] = (
                    "This is taking longer than expected. The update may still be "
                    "processing on the server — wait a minute, then check the results below."
                )
                timing.log("confirm_poll_timeout", job_id=job_id)
                return
            if job.get("status") == "error":
                st.session_state[status_key] = (
                    f"Submit failed: {job.get('error')}. Check the items and try again."
                )
                timing.log("confirm_job_error", job_id=job_id, error=job.get("error"))
                return
            results = job.get("results", [])
            timing.log("confirm_result", results=len(results))

        st.session_state[f"{key_prefix}_confirm_results"] = results
        st.session_state[items_key] = []
        if results and all(r["success"] for r in results):
            # Everything went through clean — clear the paste box so the
            # next delivery starts from empty instead of staff having to
            # select-all/delete the old text themselves. Left alone on a
            # partial failure, since the pasted text is still useful for
            # reference/retry then.
            #
            # Deferred via a flag rather than writing straight to
            # st.session_state[paste_input_key] here: the classic flow
            # (unlike SKIP REVIEW) renders the paste text_area and the
            # review table/submit button in the same script run, so by
            # the time _submit_items runs, that widget may already be
            # instantiated this run — Streamlit forbids writing to a
            # widget's session_state key after that point. render_parse_tab
            # consumes this flag at the very top of its next run, before
            # any text_area exists yet.
            st.session_state[f"{key_prefix}_paste_input_pending_clear"] = True
        # No status_key message here — the toast plus the "Submission
        # results" section below already cover success; a persistent box
        # repeating the same thing was redundant. Explicitly cleared (not
        # just left alone) so a stale prior message — e.g. "N item(s) are
        # flagged" from before this submission — doesn't linger on screen.
        st.session_state[status_key] = None
        st.toast(f"Submitted {label} with {len(payload_items)} item(s).")


def _render_paste_box(key_prefix, button_label, button_key):
    """Paste text area (with a small "Clear" button overlaid in its
    top-right corner, emptying it without staff having to select-all/
    delete by hand) + a primary action button below. Returns
    (paste_text, button_clicked).

    The Clear button uses on_click rather than checking its return value
    and setting st.session_state directly below — Streamlit forbids
    writing to a widget's session_state key after that widget has already
    been instantiated in the same script run, and the text_area above is
    instantiated first. on_click callbacks run before the script re-runs
    from the top, so the write lands before the text_area exists in that
    next run instead.
    """
    pastebox_key = f"{key_prefix}_pastebox"
    clear_key = f"{key_prefix}_clear_paste_btn"

    with st.container(key=pastebox_key):
        paste_text = st.text_area(
            "Paste WhatsApp message here",
            key=f"{key_prefix}_paste_input",
            placeholder="Paste WhatsApp message here",
            label_visibility="collapsed",
            height=150,
        )

        def _clear_paste():
            st.session_state[f"{key_prefix}_paste_input"] = ""

        st.button("Clear", key=clear_key, on_click=_clear_paste)

    # Overlays the Clear button into the text area's own top-right corner
    # instead of sitting as a separate full-size button below it — small
    # and muted (gray) so it reads as a minor, secondary action next to
    # the primary (green) submit button underneath. Top, not bottom:
    # Streamlit's own "Press ⌘+Enter to apply" hint (shown while there's
    # an uncommitted edit) renders bottom-right of the textarea, and the
    # two were colliding there.
    st.markdown(
        f"""
        <style>
        .st-key-{pastebox_key} {{ position: relative; }}
        .st-key-{pastebox_key} textarea {{ resize: none; }}
        .st-key-{clear_key} {{
            position: absolute;
            top: 0.6rem;
            right: 0.6rem;
            z-index: 5;
            width: auto;
        }}
        .st-key-{clear_key} button {{
            padding: 0.1rem 0.6rem;
            font-size: 0.75rem;
            min-height: unset;
            background-color: rgba(120, 120, 120, 0.15);
            border-color: rgba(120, 120, 120, 0.4);
            color: inherit;
        }}
        .st-key-{clear_key} button:hover {{
            background-color: rgba(120, 120, 120, 0.3);
            border-color: rgba(120, 120, 120, 0.6);
            color: inherit;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )

    clicked = st.button(button_label, key=button_key)
    return paste_text, clicked


def _run_parse_flow(mode, text):
    """POSTs /parse and drives a real st.status + progress UI for the
    result — polling GET /job/status/<job_id> if the backend hands off to
    a background job for anything needing the LLM agent (see server.py's
    /parse route). Shared by both the classic flow's "Read items" button
    and SKIP REVIEW's "Submit GRN" first click, so they can't drift apart.

    Returns (items, error_message) — exactly one of the two is falsy.
    """
    run_id = timing.new_run_id()
    with timing.section(
        "FRONTEND: READ ITEMS", run_id=run_id, mode=mode, text_chars=len(text),
        text_lines=len(text.splitlines()),
    ):
        try:
            with timing.timed("http_post_parse"):
                resp = requests.post(
                    f"{BACKEND_URL}/parse",
                    json={"text": text, "mode": mode},
                    # The heavy work now runs server-side in a background
                    # thread (see server.py) — this call just kicks it off
                    # (or, for a fully-resolved paste, returns the result
                    # directly), so it should return quickly either way.
                    timeout=30,
                )
        except Exception as e:
            timing.log("parse_exception", error=str(e))
            return None, f"Could not reach backend at {BACKEND_URL}: {e}"

        if not resp.ok:
            error = resp.json().get("error", resp.text)
            timing.log("parse_failed", error=error)
            return None, f"Couldn't process that: {error}"

        data = resp.json()

        if "job_id" not in data:
            # Everything resolved by the fast tiers — nothing to poll.
            items = data["items"]
            timing.log("parse_result", items_added=len(items))
            exact_count = sum(1 for it in items if it.get("exact_match"))
            st.status(
                f"{len(items)} item(s): {exact_count} exact match(es), "
                f"{len(items) - exact_count} need review",
                state="complete", expanded=False,
            )
            return items, None

        job_id = data["job_id"]
        total = data.get("total", 0)
        # The fast tiers (exact/cache/fuzzy match) already resolved
        # `already_resolved` of these essentially instantly — reporting
        # progress against the *full* total makes the bar jump straight to
        # e.g. 49/56 then sit frozen there for the entire real wait, since
        # only the remainder actually takes any time. Scope the bar to just
        # that remainder instead, so each real tick is a much bigger (and
        # more honest-looking) step.
        already_resolved = data.get("done") or 0
        remaining_total = max(1, total - already_resolved)

        status = st.status("Reading your list…", expanded=True)
        with status:
            if already_resolved:
                st.write(f"✓ Matched {already_resolved} of {total} item(s) instantly")
            progress_box = st.empty()
            loading_ui.render_progress(
                progress_box, 0, f"Resolving 0 of {remaining_total} remaining item(s)…",
            )

        deadline = time.time() + 600  # matches the old client-side timeout
        job = None
        while time.time() < deadline:
            try:
                poll_resp = requests.get(f"{BACKEND_URL}/job/status/{job_id}", timeout=10)
                poll_resp.raise_for_status()
                job = poll_resp.json()
            except Exception as e:
                timing.log("parse_poll_exception", error=str(e))
                time.sleep(1.5)
                continue

            remaining_done = max(0, job.get("done", 0) - already_resolved)
            loading_ui.render_progress(
                progress_box, remaining_done / remaining_total,
                f"Resolving {remaining_done} of {remaining_total} remaining item(s)…",
            )
            if job.get("status") in ("done", "error"):
                break
            time.sleep(1.5)

        if job is None or job.get("status") not in ("done", "error"):
            status.update(label="This is taking longer than expected", state="error", expanded=True)
            timing.log("parse_poll_timeout", job_id=job_id)
            return None, (
                "The list may still be processing on the server. Wait a minute and "
                "try refreshing, or submit a shorter list."
            )

        if job.get("status") == "error":
            status.update(label="Couldn't finish parsing that list", state="error", expanded=True)
            timing.log("parse_job_error", job_id=job_id, error=job.get("error"))
            return None, job.get("error") or "Something went wrong while matching your list to the catalog."

        items = job.get("items", [])
        exact_count = sum(1 for it in items if it.get("exact_match"))
        status.update(
            label=f"{len(items)} item(s): {exact_count} exact match(es), "
                  f"{len(items) - exact_count} need review",
            state="complete", expanded=False,
        )
        timing.log("parse_result", items_added=len(items))
        return items, None


def _render_grn_skip_review(
    key_prefix, mode, items_key, status_key, catalog_products,
    selected_shop_id, selected_supplier_id, show_shop_selector, show_supplier_selector,
):
    """GRN's SKIP REVIEW flow (SI_GRN_SKIP_REVIEW=true): no read-then-review
    step — a single "Submit GRN" button parses the paste and, if every item
    matched cleanly (exact or fuzzy), submits it right away. If anything
    came back with no match at all, only those flagged rows are shown for
    correction — the rest of the batch is trusted as-is — and submission is
    blocked until every flag is fixed.
    """
    pending_items = st.session_state.get(items_key) or []
    submit_busy_key = f"{key_prefix}_submit"
    submit_busy = loading_ui.is_busy(submit_busy_key)
    submit_btn_key = f"{key_prefix}_submit_btn"

    # "Submit GRN" (both instances below share this key — the correction
    # view's button and the fresh-paste one) stands out in green as the
    # one action that actually submits, distinct from Clear's muted gray.
    st.markdown(
        f"""
        <style>
        .st-key-{submit_btn_key} button {{
            background-color: #16a34a;
            border-color: #16a34a;
            color: #ffffff;
        }}
        .st-key-{submit_btn_key} button:hover {{
            background-color: #15803d;
            border-color: #15803d;
            color: #ffffff;
        }}
        .st-key-{submit_btn_key} button:disabled {{
            background-color: rgba(22, 163, 74, 0.35);
            border-color: rgba(22, 163, 74, 0.35);
            color: rgba(255, 255, 255, 0.8);
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )

    if pending_items:
        if submit_busy:
            # Replaces the correction table entirely while submitting —
            # nothing clickable on screen, and no risk of re-deriving a
            # different edited_items from the same widget keys mid-submit.
            _submit_items(
                key_prefix, items_key, status_key, loading_ui.busy_payload(submit_busy_key, "items"),
                selected_shop_id, selected_supplier_id, show_shop_selector, show_supplier_selector,
                label="GRN",
            )
            loading_ui.clear_busy(submit_busy_key, "items")
            st.rerun()
            return

        product_lookup = _product_lookup(catalog_products or [])
        edited_items, deleted_rid = _render_match_rows(
            pending_items, key_prefix, "goods_received", "Qty", product_lookup,
            compact=True, hide_statuses={"exact", "fuzzy"},
        )
        if deleted_rid is not None:
            st.session_state[items_key] = [it for it in edited_items if it["_rid"] != deleted_rid]
            st.rerun()
        st.session_state[items_key] = edited_items

        unmatched_count = sum(1 for e in edited_items if not e.get("matched_product_id"))
        if st.button(
            "Submit GRN", key=f"{key_prefix}_submit_btn", disabled=bool(unmatched_count),
        ):
            loading_ui.start_busy(submit_busy_key, items=edited_items)
            st.rerun()
        return

    if submit_busy:
        # Replaces the paste box + button entirely while parsing/submitting.
        text = loading_ui.busy_payload(submit_busy_key, "paste_text")
        new_items, error = _run_parse_flow(mode, text)
        if error is not None:
            st.session_state[status_key] = f"Couldn't process that: {error}"
        else:
            new_items = _with_row_ids(new_items)
            unmatched_count = sum(1 for it in new_items if not it.get("matched_product_id"))
            if unmatched_count:
                st.session_state[items_key] = new_items
                st.session_state[status_key] = (
                    f"Processed {len(new_items)} item(s), "
                    f"{unmatched_count}/{len(new_items)} item(s) are flagged — "
                    "please fix before proceeding."
                )
            else:
                st.session_state[items_key] = []
                _submit_items(
                    key_prefix, items_key, status_key, new_items,
                    selected_shop_id, selected_supplier_id,
                    show_shop_selector, show_supplier_selector,
                    label="GRN",
                )
        loading_ui.clear_busy(submit_busy_key, "paste_text")
        st.rerun()
        return

    paste_text, submit_clicked = _render_paste_box(
        key_prefix, "Submit GRN", f"{key_prefix}_submit_btn",
    )
    if submit_clicked and paste_text:
        loading_ui.start_busy(submit_busy_key, paste_text=paste_text)
        st.rerun()


def render_parse_tab(
    key_prefix, mode, show_shop_selector, show_supplier_selector=False,
    subheader=None, caption=None, catalog_products=None,
):
    """Shared paste -> review -> confirm flow.

    mode: "product_updates" (every item is a stock_update matched against the
    existing catalog), "new_products" (every item is a brand new product, no
    shop needed), or "goods_received" (every item is added to stock via a
    GRN — needs both a shop and a supplier).

    catalog_products: the shared, already-fetched catalog (see GET /catalog),
    passed down from the top of the script so multiple tabs in one rerun
    don't each fetch it separately.
    """
    if subheader:
        st.subheader(subheader)
    if caption:
        st.caption(caption)

    # Must run before the paste text_area is instantiated anywhere below
    # (classic or SKIP REVIEW) — see the comment on this flag's write
    # site in _submit_items for why it's deferred to here instead of
    # cleared directly at submit time.
    if st.session_state.pop(f"{key_prefix}_paste_input_pending_clear", False):
        st.session_state[f"{key_prefix}_paste_input"] = ""

    selected_shop_id = None
    selected_supplier_id = None
    if show_shop_selector or show_supplier_selector:
        supplier_options = {}
        supplier_fetch_error = None
        if show_supplier_selector:
            try:
                supplier_options = _supplier_options(
                    _session_cached("_cache_suppliers", client.list_suppliers, "Loading supplier list…")
                )
            except Exception as e:
                supplier_fetch_error = str(e)

        # Resolved here (not inside the column below) so the layout can
        # give Shop the full width when Supplier ends up contributing
        # nothing to it — otherwise Shop is stuck in a half-width column
        # next to permanently empty space.
        show_supplier_dropdown = show_supplier_selector and not (
            DEFAULT_SUPPLIER_TO_NA and "N/A" in supplier_options
        )

        if show_shop_selector and show_supplier_dropdown:
            shop_col, supplier_col = st.columns(2)
        else:
            shop_col = supplier_col = st.container()

        if show_shop_selector:
            with shop_col:
                try:
                    shop_options = _shop_options(
                        _session_cached("_cache_shops", client.list_shops, "Loading shop list…")
                    )
                except Exception as e:
                    shop_options = {}
                    st.error(f"Could not load shops: {e}")

                shop_label = st.selectbox(
                    "Shop",
                    options=list(shop_options.keys()) or ["-"],
                    key=f"{key_prefix}_shop",
                )
                selected_shop_id = shop_options.get(shop_label)

        if show_supplier_selector:
            with supplier_col:
                if supplier_fetch_error:
                    st.error(f"Could not load suppliers: {supplier_fetch_error}")

                if not show_supplier_dropdown:
                    # Always N/A in practice — no point showing a dropdown
                    # for a choice that's never actually made. Set
                    # SI_GRN_DEFAULT_SUPPLIER_TO_NA=false to bring the
                    # dropdown back.
                    selected_supplier_id = supplier_options.get("N/A")
                else:
                    supplier_keys = list(supplier_options.keys()) or ["-"]
                    # "N/A" (no tracked supplier) covers the overwhelming
                    # majority of deliveries, so default to it instead of
                    # whatever happens to sort first.
                    default_supplier_index = (
                        supplier_keys.index("N/A") if "N/A" in supplier_keys else 0
                    )
                    supplier_label = st.selectbox(
                        "Supplier",
                        options=supplier_keys,
                        index=default_supplier_index,
                        key=f"{key_prefix}_supplier",
                    )
                    selected_supplier_id = supplier_options.get(supplier_label)

    items_key = f"{key_prefix}_parsed_items"
    status_key = f"{key_prefix}_status_message"
    if status_key not in st.session_state:
        st.session_state[status_key] = None

    if st.session_state[status_key]:
        with st.container(border=True):
            st.write(st.session_state[status_key])

    skip_review = mode == "goods_received" and _env_flag("SI_GRN_SKIP_REVIEW", default="true")

    if skip_review:
        _render_grn_skip_review(
            key_prefix, mode, items_key, status_key, catalog_products,
            selected_shop_id, selected_supplier_id,
            show_shop_selector, show_supplier_selector,
        )
    else:
        read_busy_key = f"{key_prefix}_read"
        read_busy = loading_ui.is_busy(read_busy_key)

        if read_busy:
            # Replaces the paste box + button entirely while parsing.
            text = loading_ui.busy_payload(read_busy_key, "paste_text")
            new_items, error = _run_parse_flow(mode, text)
            if error is not None:
                st.session_state[status_key] = f"Couldn't process that: {error}"
            else:
                new_items = _with_row_ids(new_items)
                existing_items = st.session_state.get(items_key) or []
                st.session_state[items_key] = existing_items + new_items
                st.session_state[f"{key_prefix}_confirm_results"] = None
                st.session_state[status_key] = f"Added {len(new_items)} item(s) — review below."
            loading_ui.clear_busy(read_busy_key, "paste_text")
            st.rerun()
        else:
            paste_text, read_clicked = _render_paste_box(
                key_prefix, "Read items", f"{key_prefix}_read_btn",
            )

            if read_clicked and paste_text:
                loading_ui.start_busy(read_busy_key, paste_text=paste_text)
                st.rerun()

        items = st.session_state.get(items_key)

        if items:
            confirm_busy_key = f"{key_prefix}_confirm"
            confirm_busy = loading_ui.is_busy(confirm_busy_key)
            confirm_label = {
                "goods_received": "GRN", "product_updates": "stock update", "new_products": "new product",
            }.get(mode, "item")

            if confirm_busy:
                # Replaces the editable review table entirely while
                # submitting — nothing clickable on screen, and no risk of
                # re-deriving a different edited_items from the same
                # widget keys mid-submit.
                _submit_items(
                    key_prefix, items_key, status_key, loading_ui.busy_payload(confirm_busy_key, "items"),
                    selected_shop_id, selected_supplier_id, show_shop_selector, show_supplier_selector,
                    label=confirm_label,
                )
                loading_ui.clear_busy(confirm_busy_key, "items")
                st.rerun()
            else:
                st.markdown("---")
                st.markdown("**Review parsed items and fix anything before submitting:**")

                trust_mode = False
                if mode == "goods_received":
                    trust_mode = st.checkbox(
                        "Trust mode — only show flagged or non-exact matches",
                        value=True, key=f"{key_prefix}_trust_mode",
                    )

                edited_items = []
                deleted_rid = None

                if mode == "new_products":
                    widths = [1, 4.5, 1.1, 1.1, 0.5]
                    headers = ["Code", "Product name", "Price", "Initial stock", ""]

                    for col, header in zip(st.columns(widths), headers):
                        col.markdown(f"**{header}**")

                    for item in items:
                        rid = item["_rid"]
                        cols = st.columns(widths)
                        code = cols[0].text_input(
                            "Code", value=item.get("code") or "",
                            key=f"{key_prefix}_code_{rid}", label_visibility="collapsed",
                        )
                        name = cols[1].text_input(
                            "Product name", value=item.get("name", ""),
                            key=f"{key_prefix}_name_{rid}", label_visibility="collapsed",
                        )
                        price = cols[2].number_input(
                            "Price", value=float((item.get("fields") or {}).get("price") or 0.0),
                            min_value=0.0, step=0.01, format="%.2f",
                            key=f"{key_prefix}_price_{rid}", label_visibility="collapsed",
                        )
                        quantity = cols[3].number_input(
                            "Initial stock", value=int(item.get("quantity") or 0),
                            step=1, key=f"{key_prefix}_qty_{rid}", label_visibility="collapsed",
                        )
                        if cols[4].button("\U0001F5D1", key=f"{key_prefix}_del_{rid}"):
                            deleted_rid = rid
                        edited_items.append({
                            "_rid": rid,
                            "raw_text": item.get("raw_text", ""),
                            "action": "new_product",
                            "name": name,
                            "code": code or None,
                            "fields": {"price": price},
                            "quantity": quantity,
                            "matched_product_id": None,
                            "candidates": [],
                        })

                else:  # product_updates (stock_update) or goods_received: matched against the catalog
                    product_lookup = _product_lookup(catalog_products or [])

                    action = "goods_received" if mode == "goods_received" else "stock_update"
                    qty_label = "Qty" if mode == "goods_received" else "Qty / Δ"
                    edited_items, deleted_rid = _render_match_rows(
                        items, key_prefix, action, qty_label, product_lookup,
                        compact=(mode == "goods_received"),
                        hide_statuses={"exact"} if trust_mode else frozenset(),
                        hidden_caption=(
                            "Trust mode: {n} exact match(es) hidden — turn it off to review everything."
                            if trust_mode else None
                        ),
                    )

                if deleted_rid is not None:
                    st.session_state[items_key] = [it for it in edited_items if it["_rid"] != deleted_rid]
                    st.rerun()

                # Persist edits so a follow-up paste operates on the latest state.
                st.session_state[items_key] = edited_items

                unmatched_count = sum(
                    1 for e in edited_items
                    if e["action"] in ("stock_update", "goods_received") and not e.get("matched_product_id")
                )
                if unmatched_count:
                    st.warning(
                        f"Fix {unmatched_count} unmatched item(s) before submitting — "
                        "select a product or delete the row."
                    )

                if st.button(
                    "Confirm and Submit", key=f"{key_prefix}_confirm_btn", disabled=bool(unmatched_count)
                ):
                    missing_shop = show_shop_selector and not selected_shop_id and any(
                        e["action"] in ("stock_update", "goods_received") for e in edited_items
                    )
                    missing_supplier = show_supplier_selector and not selected_supplier_id and any(
                        e["action"] == "goods_received" for e in edited_items
                    )
                    if missing_shop:
                        st.warning("Select a shop first.")
                    elif missing_supplier:
                        st.warning("Select a supplier first.")
                    else:
                        loading_ui.start_busy(confirm_busy_key, items=edited_items)
                        st.rerun()

    results = st.session_state.get(f"{key_prefix}_confirm_results")
    if results:
        st.markdown("---")
        st.markdown("**Submission results:**")
        if all(r["success"] for r in results):
            st.success(f"✓ All {len(results)} item(s) submitted successfully.")
        else:
            for r in results:
                label = r.get("name") or r["raw_text"]
                if r["success"]:
                    st.success(f"✓ {label}")
                else:
                    st.error(f"✗ {label} — {r['error']}")


def _render_production_banner():
    """Fixed corner banner + subtle red tint, so it's unmistakable that
    actions here hit the real Sales Intellect account (not preprod)."""
    if PREPROD_URL:
        link_html = (
            f'<a href="{PREPROD_URL}" target="_blank" '
            'style="color:#fff;text-decoration:underline;font-weight:600;">'
            "Go to preprod →</a>"
        )
    else:
        link_html = '<span style="opacity:0.75;">(PREPROD_URL not set)</span>'

    st.markdown(
        f"""
        <div style="
            position: fixed; top: 0.6rem; left: 1rem; z-index: 1000000;
            background: #b91c1c; color: #fff; padding: 6px 14px;
            border-radius: 6px; font-size: 0.8rem; letter-spacing: 0.03em;
            box-shadow: 0 2px 8px rgba(0,0,0,0.35);
        ">
            \U0001F534 <strong>PRODUCTION</strong> &nbsp;|&nbsp; {link_html}
        </div>
        <style>
        [data-testid="stAppViewContainer"] {{ border-top: 4px solid #b91c1c; }}
        [data-testid="stHeader"] {{ background-color: rgba(185, 28, 28, 0.08); }}
        </style>
        """,
        unsafe_allow_html=True,
    )


st.set_page_config(page_title="Sales Intellect POS", layout="wide")
loading_ui.inject_global_css()

if IS_PRODUCTION:
    _render_production_banner()

st.title("Sales Intellect AI Wrapper", anchor=False)

try:
    client = get_client()
except ValueError as e:
    st.error(str(e))
    st.stop()

# Shared product catalog, fetched once per browser session (not on every
# rerun — see _session_cached()) and passed down to every tab below, so
# multiple enabled tabs in one rerun don't each fetch it separately.
# Served from the backend's own cache (never the live Sales Intellect API
# directly — see _fetch_catalog()); only "Refresh Catalog" below does a
# live fetch, which also updates this session's cached copy directly.
catalog_products = []
if SHOW_REFRESH_SUPPLIERS:
    catalog_col, refresh_catalog_col, refresh_suppliers_col = st.columns([4, 1, 1.2])
else:
    catalog_col, refresh_catalog_col = st.columns([4, 1])
try:
    if "_cache_catalog" not in st.session_state:
        with loading_ui.overlay("Loading product catalog…"):
            st.session_state["_cache_catalog"] = _fetch_catalog()
    catalog_data = st.session_state["_cache_catalog"]
    catalog_products = catalog_data.get("products", [])
    fetched_at = catalog_data.get("fetched_at")
    cache_found = catalog_data.get("cache_found", True)
    with catalog_col:
        if not cache_found:
            st.warning(catalog_data.get("message") or "No catalog cache found on server — click \"Refresh Catalog\".")
        else:
            st.caption(
                f"Product catalog: {len(catalog_products)} products"
                + (f" · last refreshed {_format_fetched_at(fetched_at)}" if fetched_at else "")
            )
except Exception as e:
    with catalog_col:
        st.caption(f"Could not load product catalog: {e}")

with refresh_catalog_col:
    refresh_catalog_clicked = st.button(
        "Refresh Catalog", key="global_catalog_refresh",
        disabled=loading_ui.is_busy("catalog_refresh"),
    )
if refresh_catalog_clicked and not loading_ui.is_busy("catalog_refresh"):
    loading_ui.start_busy("catalog_refresh")
    st.rerun()

if loading_ui.is_busy("catalog_refresh"):
    run_id = timing.new_run_id()
    with timing.section("FRONTEND: REFRESH CATALOG", run_id=run_id):
        try:
            with loading_ui.overlay("Refreshing catalog from Sales Intellect…"):
                with timing.timed("http_post_catalog_refresh"):
                    resp = requests.post(f"{BACKEND_URL}/catalog/refresh", timeout=120)
            if resp.ok:
                count = resp.json().get("count")
                timing.log("catalog_refresh_result", count=count)
                try:
                    # Update this session's own cached view immediately —
                    # now that the catalog is session-cached (not refetched
                    # every rerun), nothing else would ever pick up the
                    # refreshed data otherwise.
                    st.session_state["_cache_catalog"] = _fetch_catalog()
                except Exception:
                    pass  # The refresh itself still succeeded; this session's view just stays stale until reloaded.
                st.toast(f"Catalog refreshed — {count} product(s)")
            else:
                error = resp.json().get("error", resp.text)
                st.error(f"Refresh failed: {error}")
                timing.log("catalog_refresh_failed", error=error)
        except Exception as e:
            st.error(f"Could not reach backend at {BACKEND_URL}: {e}")
            timing.log("catalog_refresh_exception", error=str(e))
    loading_ui.clear_busy("catalog_refresh")
    st.rerun()

if SHOW_REFRESH_SUPPLIERS:
    with refresh_suppliers_col:
        if st.button(
            "Refresh Suppliers", key="global_suppliers_refresh",
            help="Also refreshes the shop list. Shops and suppliers rarely change, "
                 "so they're only refetched when you click this.",
        ):
            st.session_state.pop("_cache_shops", None)
            st.session_state.pop("_cache_suppliers", None)

enabled_tabs = [t for t in TAB_CONFIG if t["enabled"]]

if len(enabled_tabs) == 1:
    render_parse_tab(catalog_products=catalog_products, **enabled_tabs[0]["render_kwargs"])
elif enabled_tabs:
    tabs = st.tabs([t["label"] for t in enabled_tabs])
    for tab_widget, tab_def in zip(tabs, enabled_tabs):
        with tab_widget:
            render_parse_tab(catalog_products=catalog_products, **tab_def["render_kwargs"])
