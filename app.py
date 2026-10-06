"""Streamlit UI: paste-and-parse into Sales Intellect updates.

Run with:
    export SI_API_TOKEN=your_token_here
    export ANTHROPIC_API_KEY=your_anthropic_key
    streamlit run app.py
(or use ./run.sh to start this together with the backend)
"""

import os
import uuid

import requests
import streamlit as st

import timing
from client import SalesIntellectClient

BACKEND_URL = os.environ.get("SI_BACKEND_URL", "http://127.0.0.1:5050")


def _env_flag(name, default="false"):
    return os.environ.get(name, default).strip().lower() == "true"

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
            # Implementation notes (kept out of the staff-facing caption):
            # all items in one submission are recorded as a single GRN, and
            # payment method is fixed to Cash — the only one currently
            # supported, see _GRN_CASH_PAYMENT_METHOD_ID in server.py.
            caption=(
                "Paste the delivery message from WhatsApp. You'll check and "
                "edit every item before anything is saved. New products go "
                "in Bulk Product Upload."
            ),
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


def _with_row_ids(items):
    for item in items:
        item["_rid"] = uuid.uuid4().hex
    return items


# Match-status -> (background tint, left-border accent) for the color-coded
# name field in compact mode. Transparent backgrounds so text stays readable
# in both light and dark themes.
_MATCH_STATUS_STYLE = {
    "exact": ("rgba(34, 197, 94, 0.16)", "#22c55e"),
    "fuzzy": ("rgba(234, 179, 8, 0.18)", "#eab308"),
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
    yellow = matched but not exact, red = unmatched) via _MATCH_STATUS_STYLE.

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
            elif status == "fuzzy":
                name_cell.caption("Not an exact match — please confirm")
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


def _submit_grn_items(
    key_prefix, items_key, status_key, items,
    selected_shop_id, selected_supplier_id, show_shop_selector, show_supplier_selector,
):
    """POSTs /confirm for a batch of (already-flag-free) GRN items and
    updates status_key/confirm_results accordingly. Shared by SKIP REVIEW's
    two submission paths: immediately after a clean parse, and once a
    flagged batch has been fixed up.
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
            with st.spinner(f"Submitting {len(payload_items)} item(s)…"):
                with timing.timed("http_post_confirm"):
                    resp = requests.post(
                        f"{BACKEND_URL}/confirm",
                        json={
                            "shop_id": selected_shop_id,
                            "supplier_id": selected_supplier_id,
                            "items": payload_items,
                        },
                        timeout=300,
                    )
            if resp.ok:
                st.session_state[f"{key_prefix}_confirm_results"] = resp.json()["results"]
                st.session_state[items_key] = []
                st.session_state[status_key] = f"Submitted GRN with {len(payload_items)} item(s)."
                timing.log("confirm_result", results=len(payload_items))
            else:
                error = resp.json().get("error", resp.text)
                st.session_state[status_key] = f"Submit failed: {error}"
                timing.log("confirm_failed", error=error)
        except Exception as e:
            st.session_state[status_key] = f"Could not reach confirm backend at {BACKEND_URL}: {e}"
            timing.log("confirm_exception", error=str(e))


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

    if pending_items:
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
            _submit_grn_items(
                key_prefix, items_key, status_key, edited_items,
                selected_shop_id, selected_supplier_id, show_shop_selector, show_supplier_selector,
            )
            st.rerun()
        return

    paste_text = st.text_area(
        "Paste WhatsApp message here",
        key=f"{key_prefix}_paste_input",
        placeholder="Paste WhatsApp message here",
        label_visibility="collapsed",
        height=150,
    )

    if st.button("Submit GRN", key=f"{key_prefix}_submit_btn") and paste_text:
        run_id = timing.new_run_id()
        with timing.section(
            "FRONTEND: READ ITEMS", run_id=run_id, mode=mode, paste_chars=len(paste_text),
            paste_lines=len(paste_text.splitlines()),
        ):
            try:
                with st.spinner("Processing — large pastes can take a minute or two…"):
                    with timing.timed("http_post_parse"):
                        resp = requests.post(
                            f"{BACKEND_URL}/parse",
                            json={"text": paste_text, "mode": mode},
                            timeout=600,
                        )
                if resp.ok:
                    new_items = _with_row_ids(resp.json()["items"])
                    unmatched_count = sum(
                        1 for it in new_items if not it.get("matched_product_id")
                    )
                    timing.log(
                        "parse_result", items_added=len(new_items), unmatched=unmatched_count,
                    )
                    if unmatched_count:
                        st.session_state[items_key] = new_items
                        st.session_state[status_key] = (
                            f"Processed {len(new_items)} item(s), "
                            f"{unmatched_count}/{len(new_items)} item(s) are flagged — "
                            "please fix before proceeding."
                        )
                    else:
                        st.session_state[items_key] = []
                        _submit_grn_items(
                            key_prefix, items_key, status_key, new_items,
                            selected_shop_id, selected_supplier_id,
                            show_shop_selector, show_supplier_selector,
                        )
                else:
                    error = resp.json().get("error", resp.text)
                    st.session_state[status_key] = f"Couldn't process that: {error}"
                    timing.log("parse_failed", error=error)
            except Exception as e:
                st.session_state[status_key] = f"Could not reach backend at {BACKEND_URL}: {e}"
                timing.log("parse_exception", error=str(e))

        st.rerun()


def render_parse_tab(
    key_prefix, mode, caption, show_shop_selector, show_supplier_selector=False,
    subheader=None, catalog_products=None,
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
    st.caption(caption)

    selected_shop_id = None
    selected_supplier_id = None
    if show_shop_selector or show_supplier_selector:
        selector_cols = st.columns(2)

        if show_shop_selector:
            with selector_cols[0]:
                try:
                    shop_options = _shop_options(client.list_shops())
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
            with selector_cols[1]:
                try:
                    supplier_options = _supplier_options(client.list_suppliers())
                except Exception as e:
                    supplier_options = {}
                    st.error(f"Could not load suppliers: {e}")

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

        if mode == "goods_received":
            st.caption("Payment: Cash")

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
        paste_text = st.text_area(
            "Paste WhatsApp message here",
            key=f"{key_prefix}_paste_input",
            placeholder="Paste WhatsApp message here",
            label_visibility="collapsed",
            height=150,
        )

        if st.button("Read items", key=f"{key_prefix}_read_btn") and paste_text:
            run_id = timing.new_run_id()
            with timing.section(
                "FRONTEND: READ ITEMS", run_id=run_id, mode=mode, paste_chars=len(paste_text),
                paste_lines=len(paste_text.splitlines()),
            ):
                try:
                    # A large paste (e.g. a full delivery's worth of GRN lines) can
                    # take a couple of minutes — each line goes through several
                    # agent tool calls. 90s was too short and produced a client-side
                    # timeout even though the backend was still working correctly
                    # (confirmed: 42 items completed in ~133s, all matched).
                    with st.spinner("Parsing — large pastes can take a minute or two…"):
                        with timing.timed("http_post_parse"):
                            resp = requests.post(
                                f"{BACKEND_URL}/parse",
                                json={"text": paste_text, "mode": mode},
                                timeout=600,
                            )
                    if resp.ok:
                        new_items = _with_row_ids(resp.json()["items"])
                        existing_items = st.session_state.get(items_key) or []
                        st.session_state[items_key] = existing_items + new_items
                        st.session_state[f"{key_prefix}_confirm_results"] = None
                        st.session_state[status_key] = f"Added {len(new_items)} item(s) — review below."
                        timing.log("parse_result", items_added=len(new_items))
                    else:
                        error = resp.json().get("error", resp.text)
                        st.session_state[status_key] = f"Couldn't process that: {error}"
                        timing.log("parse_failed", error=error)
                except Exception as e:
                    st.session_state[status_key] = f"Could not reach backend at {BACKEND_URL}: {e}"
                    timing.log("parse_exception", error=str(e))

            st.rerun()

        items = st.session_state.get(items_key)

        if items:
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
                    payload_items = [{k: v for k, v in it.items() if k != "_rid"} for it in edited_items]
                    run_id = timing.new_run_id()
                    with timing.section(
                        "FRONTEND: CONFIRM AND SUBMIT", run_id=run_id, items=len(payload_items),
                    ):
                        try:
                            with st.spinner("Submitting…"):
                                with timing.timed("http_post_confirm"):
                                    resp = requests.post(
                                        f"{BACKEND_URL}/confirm",
                                        json={
                                            "shop_id": selected_shop_id,
                                            "supplier_id": selected_supplier_id,
                                            "items": payload_items,
                                        },
                                        # stock_update does two HTTP calls per item
                                        # (get_inventory + set_inventory), so a large
                                        # batch can add up — same false-timeout risk
                                        # as /parse.
                                        timeout=300,
                                    )
                            if resp.ok:
                                st.session_state[f"{key_prefix}_confirm_results"] = resp.json()["results"]
                                # Clear so the next paste starts from empty rather than
                                # appending onto items that were already submitted.
                                st.session_state[items_key] = []
                                timing.log("confirm_result", results=len(st.session_state[f"{key_prefix}_confirm_results"]))
                                st.rerun()
                            else:
                                error = resp.json().get("error", resp.text)
                                st.error(f"Confirm failed: {error}")
                                timing.log("confirm_failed", error=error)
                        except Exception as e:
                            st.error(f"Could not reach confirm backend at {BACKEND_URL}: {e}")
                            timing.log("confirm_exception", error=str(e))

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

if IS_PRODUCTION:
    _render_production_banner()

st.title("Sales Intellect POS", anchor=False)

try:
    client = get_client()
except ValueError as e:
    st.error(str(e))
    st.stop()

# Shared product catalog, fetched once per rerun from the backend's cache
# (never the live Sales Intellect API directly — see _fetch_catalog()) and
# passed down to every tab below, so multiple enabled tabs in one rerun
# don't each fetch it separately.
catalog_products = []
catalog_col, refresh_col = st.columns([5, 1])
try:
    catalog_data = _fetch_catalog()
    catalog_products = catalog_data.get("products", [])
    fetched_at = catalog_data.get("fetched_at")
    cache_found = catalog_data.get("cache_found", True)
    with catalog_col:
        if not cache_found:
            st.warning(catalog_data.get("message") or "No catalog cache found on server — click \"Refresh Catalog\".")
        else:
            st.caption(
                f"Product catalog: {len(catalog_products)} products"
                + (f" · last refreshed {fetched_at}" if fetched_at else "")
            )
except Exception as e:
    with catalog_col:
        st.caption(f"Could not load product catalog: {e}")

with refresh_col:
    if st.button("Refresh Catalog", key="global_catalog_refresh"):
        run_id = timing.new_run_id()
        with timing.section("FRONTEND: REFRESH CATALOG", run_id=run_id):
            try:
                with timing.timed("http_post_catalog_refresh"):
                    resp = requests.post(f"{BACKEND_URL}/catalog/refresh", timeout=120)
                if resp.ok:
                    timing.log("catalog_refresh_result", count=resp.json().get("count"))
                else:
                    error = resp.json().get("error", resp.text)
                    st.error(f"Refresh failed: {error}")
                    timing.log("catalog_refresh_failed", error=error)
            except Exception as e:
                st.error(f"Could not reach backend at {BACKEND_URL}: {e}")
                timing.log("catalog_refresh_exception", error=str(e))
        st.rerun()

enabled_tabs = [t for t in TAB_CONFIG if t["enabled"]]

if len(enabled_tabs) == 1:
    render_parse_tab(catalog_products=catalog_products, **enabled_tabs[0]["render_kwargs"])
elif enabled_tabs:
    tabs = st.tabs([t["label"] for t in enabled_tabs])
    for tab_widget, tab_def in zip(tabs, enabled_tabs):
        with tab_widget:
            render_parse_tab(catalog_products=catalog_products, **tab_def["render_kwargs"])
