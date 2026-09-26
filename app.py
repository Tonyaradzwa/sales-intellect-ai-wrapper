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

from client import SalesIntellectClient

BACKEND_URL = os.environ.get("SI_BACKEND_URL", "http://127.0.0.1:5050")

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
        if s.get("supplier_id") and s.get("supplier_id") != label:
            label = f"{label} ({s['supplier_id']})"
        options[label] = supplier_id
    return options


def _product_lookup(products):
    """id -> {"name", "code"}, from a raw list_products() response."""
    if isinstance(products, dict):
        for value in products.values():
            if isinstance(value, list):
                products = value
                break
    lookup = {}
    for p in products:
        lookup[p.get("id")] = {"name": p.get("product_name"), "code": p.get("product_code")}
    return lookup


def _with_row_ids(items):
    for item in items:
        item["_rid"] = uuid.uuid4().hex
    return items


def _render_match_rows(items, key_prefix, action, qty_label, product_lookup):
    """Shared review-table rendering for both product_updates (stock_update)
    and goods_received: a dropdown to confirm/override the matched product,
    its code, a quantity field, and a delete button.
    """
    all_options = sorted(
        ((pid, f"{info['name']} ({info['code']})") for pid, info in product_lookup.items()),
        key=lambda pair: pair[1],
    )

    widths = [4.5, 1.1, 1, 2, 0.5]
    headers = ["Product / Match", "Code", qty_label, "Flag", ""]

    for col, header in zip(st.columns(widths), headers):
        col.markdown(f"**{header}**")

    edited_items = []
    deleted_rid = None

    for item in items:
        rid = item["_rid"]
        cols = st.columns(widths)

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

        chosen_label = cols[0].selectbox(
            "Matched product", labels_ordered, index=default_index,
            key=f"{key_prefix}_match_{rid}", label_visibility="collapsed",
        )
        chosen_id = ids_ordered[labels_ordered.index(chosen_label)]

        code = product_lookup.get(chosen_id, {}).get("code", "")
        cols[1].text_input(
            "Code", value=code, key=f"{key_prefix}_code_display_{rid}",
            label_visibility="collapsed", disabled=True,
        )

        qty = cols[2].number_input(
            qty_label, value=int(item.get("quantity") or 0),
            step=1, key=f"{key_prefix}_qty_{rid}", label_visibility="collapsed",
        )

        if chosen_id is None:
            cols[3].caption(f"⚠️ Not found: “{item.get('raw_text', '')}”")

        if cols[4].button("\U0001F5D1", key=f"{key_prefix}_del_{rid}"):
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
        })

    return edited_items, deleted_rid


def render_parse_tab(key_prefix, mode, caption, show_shop_selector, show_supplier_selector=False, subheader=None):
    """Shared paste -> review -> confirm flow.

    mode: "product_updates" (every item is a stock_update matched against the
    existing catalog), "new_products" (every item is a brand new product, no
    shop needed), or "goods_received" (every item is added to stock via a
    GRN — needs both a shop and a supplier).
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

                supplier_label = st.selectbox(
                    "Supplier",
                    options=list(supplier_options.keys()) or ["-"],
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

    paste_text = st.text_area(
        "Paste WhatsApp message here",
        key=f"{key_prefix}_paste_input",
        placeholder="Paste WhatsApp message here",
        label_visibility="collapsed",
        height=150,
    )

    if st.button("Read items", key=f"{key_prefix}_read_btn") and paste_text:
        try:
            # A large paste (e.g. a full delivery's worth of GRN lines) can
            # take a couple of minutes — each line goes through several
            # agent tool calls. 90s was too short and produced a client-side
            # timeout even though the backend was still working correctly
            # (confirmed: 42 items completed in ~133s, all matched).
            with st.spinner("Parsing — large pastes can take a minute or two…"):
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
            else:
                st.session_state[status_key] = f"Couldn't process that: {resp.json().get('error', resp.text)}"
        except Exception as e:
            st.session_state[status_key] = f"Could not reach backend at {BACKEND_URL}: {e}"

        st.rerun()

    items = st.session_state.get(items_key)

    if items:
        st.markdown("---")
        st.markdown("**Review parsed items and fix anything before submitting:**")

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
            try:
                product_lookup = _product_lookup(client.list_products())
            except Exception as e:
                product_lookup = {}
                st.error(f"Could not load product catalog: {e}")

            action = "goods_received" if mode == "goods_received" else "stock_update"
            qty_label = "Qty" if mode == "goods_received" else "Qty / Δ"
            edited_items, deleted_rid = _render_match_rows(
                items, key_prefix, action, qty_label, product_lookup
            )

        if deleted_rid is not None:
            st.session_state[items_key] = [it for it in edited_items if it["_rid"] != deleted_rid]
            st.rerun()

        # Persist edits so a follow-up paste operates on the latest state.
        st.session_state[items_key] = edited_items

        if st.button("Confirm and Submit", key=f"{key_prefix}_confirm_btn"):
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
                try:
                    with st.spinner("Submitting…"):
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
                    else:
                        st.error(f"Confirm failed: {resp.json().get('error', resp.text)}")
                except Exception as e:
                    st.error(f"Could not reach confirm backend at {BACKEND_URL}: {e}")

    results = st.session_state.get(f"{key_prefix}_confirm_results")
    if results:
        st.markdown("---")
        st.markdown("**Submission results:**")
        for r in results:
            label = r.get("name") or r["raw_text"]
            if r["success"]:
                st.success(f"✓ {label}")
            else:
                st.error(f"✗ {label} — {r['error']}")


st.set_page_config(page_title="Sales Intellect POS", layout="wide")
st.title("Sales Intellect POS", anchor=False)

try:
    client = get_client()
except ValueError as e:
    st.error(str(e))
    st.stop()

enabled_tabs = [t for t in TAB_CONFIG if t["enabled"]]

if len(enabled_tabs) == 1:
    render_parse_tab(**enabled_tabs[0]["render_kwargs"])
elif enabled_tabs:
    tabs = st.tabs([t["label"] for t in enabled_tabs])
    for tab_widget, tab_def in zip(tabs, enabled_tabs):
        with tab_widget:
            render_parse_tab(**tab_def["render_kwargs"])
