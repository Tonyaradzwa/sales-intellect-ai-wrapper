"""Tests for the GRN date field (core.py's render_parse_tab): it defaults
to today but is editable, and whatever's selected ends up in the /confirm
payload's grn_date key.
"""

import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

from streamlit.testing.v1 import AppTest  # noqa: E402

import client as client_module  # noqa: E402

SHOPS = [{"id": "shop-a", "shop_name": "Branch A"}]
SUPPLIERS_WITH_NA = [{"id": "sup-na", "supplier_name": "N/A", "supplier_id": "COM1"}]
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


def test_grn_date_defaults_to_today(confirm_payloads):
    at = AppTest.from_file(str(APP_DIR / "app.py"))
    at.run()
    at.switch_page("pages/goods_received.py")
    at.run()
    assert not at.exception, at.exception

    date_input = at.date_input(key="grn_date")
    assert date_input.value == date.today()

    at.selectbox(key="grn_shop").select("Branch A").run()
    at.text_area(key="grn_paste_input").set_value("anything")
    at.button(key="grn_submit_btn").click().run()
    assert not at.exception, at.exception

    assert len(confirm_payloads) == 1
    assert confirm_payloads[0]["grn_date"] == date.today().isoformat()


def test_grn_date_can_be_changed(confirm_payloads):
    at = AppTest.from_file(str(APP_DIR / "app.py"))
    at.run()
    at.switch_page("pages/goods_received.py")
    at.run()
    assert not at.exception, at.exception

    backdated = date.today() - timedelta(days=3)
    at.date_input(key="grn_date").set_value(backdated).run()

    at.selectbox(key="grn_shop").select("Branch A").run()
    at.text_area(key="grn_paste_input").set_value("anything")
    at.button(key="grn_submit_btn").click().run()
    assert not at.exception, at.exception

    assert len(confirm_payloads) == 1
    assert confirm_payloads[0]["grn_date"] == backdated.isoformat()
