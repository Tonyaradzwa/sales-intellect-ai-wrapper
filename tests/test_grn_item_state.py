"""Regression test for: GRN line items from one branch leaking into the next
branch's submission (session state wasn't cleared after a successful
Confirm and Submit).

Mocks the Sales Intellect client and the /parse and /confirm HTTP calls, then
drives app.py through Streamlit's AppTest: submit a GRN for branch A, then a
second GRN for branch B, and assert branch B's submission contains only its
own items.
"""

import os
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

from streamlit.testing.v1 import AppTest  # noqa: E402

import client as client_module  # noqa: E402

SHOPS = [
    {"id": "shop-a", "shop_name": "Branch A"},
    {"id": "shop-b", "shop_name": "Branch B"},
]
SUPPLIERS = [{"id": "sup-1", "supplier_name": "Supplier One"}]
PRODUCTS = [
    {"id": "p1", "product_name": "Item A1", "product_code": "A001"},
    {"id": "p2", "product_name": "Item A2", "product_code": "A002"},
    {"id": "p3", "product_name": "Item B1", "product_code": "B001"},
]

# Shape returned by GET /catalog (server.py's get_catalog()): product_id/
# name/code/cost, not the raw Sales Intellect API's id/product_name/
# product_code — app.py now fetches the catalog from the backend's cache
# instead of calling SalesIntellectClient.list_products() directly.
CATALOG_PRODUCTS = [
    {"product_id": p["id"], "name": p["product_name"], "code": p["product_code"], "cost": 0}
    for p in PRODUCTS
]

PARSE_RESPONSES = {
    "PASTE_A": [
        {"raw_text": "Item A1 x5", "matched_product_id": "p1", "quantity": 5,
         "candidates": [], "name": "Item A1", "fields": {}, "code": "A001"},
        {"raw_text": "Item A2 x3", "matched_product_id": "p2", "quantity": 3,
         "candidates": [], "name": "Item A2", "fields": {}, "code": "A002"},
    ],
    "PASTE_B": [
        {"raw_text": "Item B1 x7", "matched_product_id": "p3", "quantity": 7,
         "candidates": [], "name": "Item B1", "fields": {}, "code": "B001"},
    ],
    "PASTE_UNMATCHED": [
        {"raw_text": "Some Unknown Thing x2", "matched_product_id": None, "quantity": 2,
         "candidates": [], "name": "", "fields": {}, "code": None},
    ],
}


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


@pytest.fixture
def confirm_payloads(monkeypatch):
    """Patches requests.post for /parse and /confirm; returns the list of
    JSON bodies sent to /confirm, in call order."""
    os.environ["SI_API_TOKEN"] = "test-token"
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    os.environ["SI_TAB_GOODS_RECEIVED"] = "true"
    os.environ["SI_TAB_PRODUCT_UPDATES"] = "false"
    os.environ["SI_TAB_BULK_UPLOAD"] = "false"
    # These tests exercise the classic read-items -> review -> confirm flow
    # specifically (grn_read_btn/grn_confirm_btn) — SKIP REVIEW has its own
    # suite in test_grn_skip_review.py.
    os.environ["SI_GRN_SKIP_REVIEW"] = "false"

    monkeypatch.setattr(client_module.SalesIntellectClient, "list_shops", lambda self: SHOPS)
    monkeypatch.setattr(client_module.SalesIntellectClient, "list_suppliers", lambda self: SUPPLIERS)
    monkeypatch.setattr(client_module.SalesIntellectClient, "list_products", lambda self: PRODUCTS)

    captured = []

    def fake_post(url, json=None, timeout=None):
        if url.endswith("/parse"):
            return FakeResponse(200, {"items": PARSE_RESPONSES[json["text"]]})
        if url.endswith("/confirm"):
            captured.append(json)
            results = [
                {"raw_text": it["raw_text"], "name": it.get("name"), "success": True, "result": {}}
                for it in json["items"]
            ]
            return FakeResponse(200, {"results": results})
        raise AssertionError(f"Unexpected URL: {url}")

    def fake_get(url, timeout=None):
        if url.endswith("/catalog"):
            return FakeResponse(200, {
                "products": CATALOG_PRODUCTS,
                "fetched_at": "2026-01-01T00:00:00",
                "count": len(CATALOG_PRODUCTS),
            })
        raise AssertionError(f"Unexpected URL: {url}")

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)

    return captured


def _submit_one_grn(at, shop_label, paste_text):
    at.selectbox(key="grn_shop").select(shop_label).run()
    at.selectbox(key="grn_supplier").select("Supplier One").run()
    at.text_area(key="grn_paste_input").set_value(paste_text)
    at.button(key="grn_read_btn").click().run()
    at.button(key="grn_confirm_btn").click().run()


def test_second_branch_grn_does_not_include_first_branchs_items(confirm_payloads):
    at = AppTest.from_file(str(APP_DIR / "app.py"))
    at.run()

    _submit_one_grn(at, "Branch A", "PASTE_A")
    assert len(confirm_payloads) == 1
    assert confirm_payloads[0]["shop_id"] == "shop-a"
    assert {it["raw_text"] for it in confirm_payloads[0]["items"]} == {"Item A1 x5", "Item A2 x3"}

    # The bug: this used to still contain branch A's items too.
    _submit_one_grn(at, "Branch B", "PASTE_B")
    assert len(confirm_payloads) == 2
    assert confirm_payloads[1]["shop_id"] == "shop-b"
    branch_b_raw_texts = {it["raw_text"] for it in confirm_payloads[1]["items"]}
    assert branch_b_raw_texts == {"Item B1 x7"}, (
        f"Branch B's GRN should contain only its own item, got: {branch_b_raw_texts}"
    )

    # Session state itself should be empty after a successful submit.
    assert at.session_state["grn_parsed_items"] == []
    # Paste box clears itself on a fully-successful submit too (classic
    # flow renders the paste box and the confirm button in the same run,
    # unlike SKIP REVIEW — this is the trickier case to get right).
    assert at.text_area(key="grn_paste_input").value == ""


def test_clear_button_empties_the_paste_box(confirm_payloads):
    at = AppTest.from_file(str(APP_DIR / "app.py"))
    at.run()

    at.text_area(key="grn_paste_input").set_value("some pasted text").run()
    assert at.text_area(key="grn_paste_input").value == "some pasted text"

    at.button(key="grn_clear_paste_btn").click().run()
    assert not at.exception, at.exception
    assert at.text_area(key="grn_paste_input").value == ""
    assert len(confirm_payloads) == 0


def test_failed_confirm_keeps_items(confirm_payloads, monkeypatch):
    """A failed /confirm must not lose the staff member's pasted work."""
    import requests

    def failing_post(url, json=None, timeout=None):
        if url.endswith("/parse"):
            return FakeResponse(200, {"items": PARSE_RESPONSES[json["text"]]})
        if url.endswith("/confirm"):
            return FakeResponse(500, {"error": "backend exploded"})
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(requests, "post", failing_post)

    at = AppTest.from_file(str(APP_DIR / "app.py"))
    at.run()

    at.selectbox(key="grn_shop").select("Branch A").run()
    at.selectbox(key="grn_supplier").select("Supplier One").run()
    at.text_area(key="grn_paste_input").set_value("PASTE_A")
    at.button(key="grn_read_btn").click().run()
    at.button(key="grn_confirm_btn").click().run()

    assert len(at.session_state["grn_parsed_items"]) == 2


def test_unmatched_item_blocks_submit(confirm_payloads):
    """An item with no matched product (staff never picked one from the
    dropdown) must disable Confirm and Submit, not silently get dropped by
    the backend's own incomplete-item check."""
    from streamlit.testing.v1.errors import AppTestError

    at = AppTest.from_file(str(APP_DIR / "app.py"))
    at.run()

    at.selectbox(key="grn_shop").select("Branch A").run()
    at.selectbox(key="grn_supplier").select("Supplier One").run()
    at.text_area(key="grn_paste_input").set_value("PASTE_UNMATCHED")
    at.button(key="grn_read_btn").click().run()

    assert at.button(key="grn_confirm_btn").proto.disabled is True
    with pytest.raises(AppTestError):
        at.button(key="grn_confirm_btn").click()

    assert len(confirm_payloads) == 0
