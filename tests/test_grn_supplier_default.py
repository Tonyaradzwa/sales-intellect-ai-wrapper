"""Tests for SI_GRN_DEFAULT_SUPPLIER_TO_NA (app.py): when "N/A" is present
in the supplier list and the flag is on (the default), the Supplier
dropdown is hidden entirely and the submission silently uses N/A's id —
instead of making staff pick a supplier that's always the same answer.
"""

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

from streamlit.testing.v1 import AppTest  # noqa: E402

import client as client_module  # noqa: E402

SHOPS = [{"id": "shop-a", "shop_name": "Branch A"}]
SUPPLIERS_WITH_NA = [
    {"id": "sup-na", "supplier_name": "N/A", "supplier_id": "COM1"},
    {"id": "sup-1", "supplier_name": "Acme Co", "supplier_id": "ACME"},
]
CATALOG_PRODUCTS = [{"product_id": "p1", "name": "Item A1", "code": "A001", "cost": 0}]

PARSE_CLEAN = [
    {"raw_text": "Item A1 x5", "matched_product_id": "p1", "quantity": 5,
     "candidates": [], "name": "Item A1", "fields": {}, "code": "A001", "exact_match": True},
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
def confirm_payloads(monkeypatch):
    os.environ["SI_API_TOKEN"] = "test-token"
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    os.environ["SI_TAB_GOODS_RECEIVED"] = "true"
    os.environ["SI_TAB_PRODUCT_UPDATES"] = "false"
    os.environ["SI_TAB_BULK_UPLOAD"] = "false"
    os.environ["SI_GRN_SKIP_REVIEW"] = "true"

    monkeypatch.setattr(client_module.SalesIntellectClient, "list_shops", lambda self: SHOPS)
    monkeypatch.setattr(client_module.SalesIntellectClient, "list_suppliers", lambda self: SUPPLIERS_WITH_NA)

    captured = []

    def fake_post(url, json=None, timeout=None):
        if url.endswith("/parse"):
            return FakeResponse(200, {"items": PARSE_CLEAN})
        if url.endswith("/confirm"):
            captured.append(json)
            results = [{"raw_text": it["raw_text"], "name": it.get("name"), "success": True, "result": {}}
                       for it in json["items"]]
            return FakeResponse(200, {"results": results})
        raise AssertionError(url)

    def fake_get(url, timeout=None):
        if url.endswith("/catalog"):
            return FakeResponse(200, {"products": CATALOG_PRODUCTS, "fetched_at": "x", "count": 1})
        raise AssertionError(url)

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)

    return captured


def test_supplier_dropdown_hidden_and_na_used_by_default(confirm_payloads, monkeypatch):
    monkeypatch.delenv("SI_GRN_DEFAULT_SUPPLIER_TO_NA", raising=False)  # exercise the default (true)

    at = AppTest.from_file(str(APP_DIR / "app.py"))
    at.run()
    assert not at.exception, at.exception

    assert not any(s.key == "grn_supplier" for s in at.selectbox)

    at.selectbox(key="grn_shop").select("Branch A").run()
    at.text_area(key="grn_paste_input").set_value("anything")
    at.button(key="grn_submit_btn").click().run()
    assert not at.exception, at.exception

    assert len(confirm_payloads) == 1
    assert confirm_payloads[0]["supplier_id"] == "sup-na"


def test_supplier_dropdown_shown_when_flag_disabled(confirm_payloads, monkeypatch):
    monkeypatch.setenv("SI_GRN_DEFAULT_SUPPLIER_TO_NA", "false")

    at = AppTest.from_file(str(APP_DIR / "app.py"))
    at.run()
    assert not at.exception, at.exception

    supplier_select = at.selectbox(key="grn_supplier")
    assert supplier_select is not None
    assert supplier_select.value == "N/A"  # still defaults to N/A, just not forced

    at.selectbox(key="grn_shop").select("Branch A").run()
    supplier_select.select("Acme Co (ACME)").run()
    at.text_area(key="grn_paste_input").set_value("anything")
    at.button(key="grn_submit_btn").click().run()
    assert not at.exception, at.exception

    assert len(confirm_payloads) == 1
    assert confirm_payloads[0]["supplier_id"] == "sup-1"


def test_refresh_suppliers_button_hidden_by_default(confirm_payloads, monkeypatch):
    monkeypatch.delenv("SI_SHOW_REFRESH_SUPPLIERS", raising=False)  # exercise the default (false)

    at = AppTest.from_file(str(APP_DIR / "app.py"))
    at.run()
    assert not at.exception, at.exception

    labels = [b.label for b in at.button]
    assert "Refresh Catalog" in labels
    assert "Refresh Suppliers" not in labels


def test_refresh_suppliers_button_shown_when_flag_enabled(confirm_payloads, monkeypatch):
    monkeypatch.setenv("SI_SHOW_REFRESH_SUPPLIERS", "true")

    at = AppTest.from_file(str(APP_DIR / "app.py"))
    at.run()
    assert not at.exception, at.exception

    labels = [b.label for b in at.button]
    assert "Refresh Suppliers" in labels
