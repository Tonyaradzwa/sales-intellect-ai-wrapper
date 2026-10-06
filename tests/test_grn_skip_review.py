"""Tests for GRN's SKIP REVIEW flow (SI_GRN_SKIP_REVIEW=true, the default).

No read-items -> review -> confirm steps: a single "Submit GRN" button
parses the paste and submits immediately if every item matched. If anything
came back unmatched, only those flagged rows are shown for correction and
submission is blocked until they're fixed — the rest of the batch (exact and
fuzzy matches) is trusted and submitted untouched.
"""

import os
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import client as client_module  # noqa: E402

SHOPS = [{"id": "shop-a", "shop_name": "Branch A"}]
SUPPLIERS = [{"id": "sup-1", "supplier_name": "Supplier One"}]
CATALOG_PRODUCTS = [{"product_id": "p1", "name": "Item A1", "code": "A001", "cost": 0}]

PARSE_CLEAN = [
    {"raw_text": "Item A1 x5", "matched_product_id": "p1", "quantity": 5,
     "candidates": [], "name": "Item A1", "fields": {}, "code": "A001", "exact_match": True},
    {"raw_text": "Itme A1 x3 (typo)", "matched_product_id": "p1", "quantity": 3,
     "candidates": [{"product_id": "p1", "name": "Item A1"}], "name": "Item A1",
     "fields": {}, "code": "A001", "exact_match": False},
]
PARSE_DIRTY = PARSE_CLEAN + [
    {"raw_text": "Totally Unknown Thing x2", "matched_product_id": None, "quantity": 2,
     "candidates": [], "name": "", "fields": {}, "code": None, "exact_match": False},
]


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._payload

    @property
    def text(self):
        return str(self._payload)

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(self._payload)


@pytest.fixture
def grn_skip_review(monkeypatch):
    """Patches requests.get/post and SalesIntellectClient for the SKIP
    REVIEW suite. Returns a dict the test fills with parse_items (what
    /parse should return) and reads confirmed (the /confirm payloads sent,
    in order) from.
    """
    os.environ["SI_API_TOKEN"] = "test-token"
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    os.environ["SI_TAB_GOODS_RECEIVED"] = "true"
    os.environ["SI_TAB_PRODUCT_UPDATES"] = "false"
    os.environ["SI_TAB_BULK_UPLOAD"] = "false"
    os.environ["SI_GRN_SKIP_REVIEW"] = "true"

    monkeypatch.setattr(client_module.SalesIntellectClient, "list_shops", lambda self: SHOPS)
    monkeypatch.setattr(client_module.SalesIntellectClient, "list_suppliers", lambda self: SUPPLIERS)

    state = {"parse_items": [], "confirmed": []}

    def fake_post(url, json=None, timeout=None):
        if url.endswith("/parse"):
            return FakeResponse(200, {"items": state["parse_items"]})
        if url.endswith("/confirm"):
            state["confirmed"].append(json)
            results = [
                {"raw_text": it["raw_text"], "name": it.get("name"), "success": True, "result": {}}
                for it in json["items"]
            ]
            return FakeResponse(200, {"results": results})
        raise AssertionError(f"Unexpected URL: {url}")

    def fake_get(url, timeout=None):
        if url.endswith("/catalog"):
            return FakeResponse(200, {
                "products": CATALOG_PRODUCTS, "fetched_at": "2026-01-01T00:00:00",
                "count": len(CATALOG_PRODUCTS),
            })
        raise AssertionError(f"Unexpected URL: {url}")

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)

    return state


def _start(at):
    at.selectbox(key="grn_shop").select("Branch A").run()
    at.selectbox(key="grn_supplier").select("Supplier One").run()


def test_clean_parse_submits_immediately_with_no_read_or_confirm_buttons(grn_skip_review):
    from streamlit.testing.v1 import AppTest

    grn_skip_review["parse_items"] = PARSE_CLEAN
    at = AppTest.from_file(str(APP_DIR / "app.py"))
    at.run()
    _start(at)

    labels = [b.label for b in at.button]
    assert "Read items" not in labels
    assert "Confirm and Submit" not in labels
    assert "Submit GRN" in labels

    at.text_area(key="grn_paste_input").set_value("anything")
    at.button(key="grn_submit_btn").click().run()
    assert not at.exception, at.exception

    assert len(grn_skip_review["confirmed"]) == 1
    assert len(grn_skip_review["confirmed"][0]["items"]) == 2
    assert at.session_state["grn_status_message"] == "Submitted GRN with 2 item(s)."
    assert at.session_state["grn_parsed_items"] == []


def test_flagged_item_blocks_submit_until_fixed_then_submits_full_batch(grn_skip_review):
    from streamlit.testing.v1 import AppTest

    grn_skip_review["parse_items"] = PARSE_DIRTY
    at = AppTest.from_file(str(APP_DIR / "app.py"))
    at.run()
    _start(at)

    at.text_area(key="grn_paste_input").set_value("anything")
    at.button(key="grn_submit_btn").click().run()
    assert not at.exception, at.exception

    # Nothing submitted yet, and the status message calls out the flag.
    assert len(grn_skip_review["confirmed"]) == 0
    status = at.session_state["grn_status_message"]
    assert "Processed 3 item(s)" in status
    assert "1/3 item(s) are flagged" in status

    # Only the unmatched row is rendered — the exact + fuzzy matches stay hidden.
    match_selects = [s for s in at.selectbox if s.key and s.key.startswith("grn_match_")]
    assert len(match_selects) == 1

    submit_btn = at.button(key="grn_submit_btn")
    assert submit_btn.disabled is True

    # Fix the flag by picking a product.
    match_selects[0].select(match_selects[0].options[1]).run()
    assert at.button(key="grn_submit_btn").disabled is False

    at.button(key="grn_submit_btn").click().run()
    assert not at.exception, at.exception

    assert len(grn_skip_review["confirmed"]) == 1
    # All 3 items go through, including the 2 that were never shown to staff.
    assert len(grn_skip_review["confirmed"][0]["items"]) == 3
    assert at.session_state["grn_status_message"] == "Submitted GRN with 3 item(s)."
    assert at.session_state["grn_parsed_items"] == []
