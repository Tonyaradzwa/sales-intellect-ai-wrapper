"""Tests for the GRN observability changes: events.log records, the attempt_id
carried from paste to server, and the no-partial-GRN rule (server refuses,
page flags missing quantities first).
"""

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import events  # noqa: E402
import server  # noqa: E402
from test_grn_skip_review import (  # noqa: E402,F401
    PARSE_CLEAN, FakeResponse, _start, grn_skip_review,
)

PARSE_NO_QTY = [
    {"raw_text": "Item A1 x5", "matched_product_id": "p1", "quantity": 5,
     "candidates": [], "name": "Item A1", "fields": {}, "code": "A001", "exact_match": True},
    {"raw_text": "Swim Dress Lace Yellow XL", "matched_product_id": "p1", "quantity": None,
     "candidates": [], "name": "Item A1", "fields": {}, "code": "A001", "exact_match": True},
]


@pytest.fixture
def event_log():
    """Empties events.log and returns a reader for what the test wrote."""
    path = Path(events._LOG_PATH)
    path.write_text("")

    def read(name=None):
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return [r for r in rows if name is None or r["event"] == name]

    return read


@pytest.fixture
def fake_si_client(monkeypatch):
    fake = mock.Mock()
    fake.list_shops.return_value = [{"id": "shop-1", "shop_name": "SPEKE"}]
    monkeypatch.setattr(server, "si_client", fake)
    return fake


def _item(raw, qty, pid="p1"):
    return {
        "action": "goods_received", "raw_text": raw, "name": raw,
        "matched_product_id": pid, "quantity": qty,
    }


def _post_confirm(items, headers=None):
    return server.app.test_client().post(
        "/confirm", json={"shop_id": "shop-1", "supplier_id": "sup-1", "items": items},
        headers=headers or {},
    )


def test_incomplete_line_refuses_whole_grn_and_names_the_lines(fake_si_client, event_log):
    resp = _post_confirm(
        [_item("Dress XL", 2), _item("Swim Dress Lace Yellow XL", None), _item("Swim Dress Lace Yellow 2XL", None)],
        headers={"X-Request-ID": "att-test01"},
    )

    assert resp.status_code == 422
    body = resp.get_json()
    assert body["error"] == (
        "Nothing was saved. 2 line(s) have no quantity: "
        "Swim Dress Lace Yellow XL, Swim Dress Lace Yellow 2XL."
    )
    fake_si_client.create_grn.assert_not_called()
    assert resp.headers["X-Request-ID"] == "att-test01"

    (refused,) = event_log("grn_refused")
    assert refused["layer"] == "api"
    assert refused["attempt_id"] == "att-test01"
    assert refused["shop"] == "SPEKE"
    assert (refused["lines_sent"], refused["lines_complete"], refused["lines_incomplete"]) == (3, 1, 2)
    assert refused["grn_created"] is False
    assert [e["server_reason"] for e in refused["incomplete"]] == ["quantity_missing"] * 2
    assert event_log("grn_created") == []


def test_unmatched_line_also_refuses_whole_grn(fake_si_client, event_log):
    resp = _post_confirm([_item("Known", 2), _item("Mystery", 1, pid=None)])

    assert resp.status_code == 422
    assert "1 line(s) have no matching product: Mystery." in resp.get_json()["error"]
    fake_si_client.create_grn.assert_not_called()


def _catalog(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "CATALOG_CACHE_PATH", str(tmp_path / "none.json"))
    products = [{"product_id": "p1", "name": "Item", "code": "A1", "cost": 1}]
    monkeypatch.setattr(server, "_catalog_state", {
        "products": products, "fetched_at": "x", "index": server.CatalogIndex(products), "cache_mtime": None,
    })


def test_grn_created_is_logged_without_any_grn_lookup(fake_si_client, event_log, monkeypatch, tmp_path):
    _catalog(monkeypatch, tmp_path)
    fake_si_client.create_grn.return_value = {"success": {"code": "SUCCESS"}}

    resp = _post_confirm([_item("A", 1), _item("B", 2)], headers={"X-Request-ID": "att-test02"})

    assert resp.status_code == 200
    fake_si_client.get_grn.assert_not_called()  # the server no longer downloads GRNs to find a number
    (created,) = event_log("grn_created")
    assert created["attempt_id"] == "att-test02" and created["lines_sent"] == 2
    assert created["si_ack"] == "SUCCESS" and "grn_number" not in created


def test_identical_payload_to_previous_grn_asks_before_creating(fake_si_client, event_log, monkeypatch, tmp_path):
    _catalog(monkeypatch, tmp_path)
    fake_si_client.create_grn.return_value = {"success": {}}
    lines = [_item("A", 1, pid="p1"), _item("B", 2, pid="p2")]

    assert _post_confirm(lines, headers={"X-Request-ID": "att-first"}).status_code == 200

    # Same lines again (order shuffled): not created, server says why.
    again = _post_confirm(list(reversed(lines)), headers={"X-Request-ID": "att-second"})
    assert again.status_code == 409
    body = again.get_json()
    assert body["duplicate"] is True
    assert body["previous"]["lines"] == 2 and body["previous"]["attempt_id"] == "att-first"
    assert "identical to the last GRN submitted for SPEKE" in body["error"] and "Submit again?" in body["error"]
    assert fake_si_client.create_grn.call_count == 1
    (dup,) = event_log("duplicate_submission")
    assert dup["attempt_id"] == "att-second" and dup["matches_attempt_id"] == "att-first"
    assert dup["action"] == "asked_user"

    # The user says it's intentional: goes through, and is logged as such.
    confirmed = server.app.test_client().post("/confirm", json={
        "shop_id": "shop-1", "supplier_id": "sup-1", "items": lines, "confirm_duplicate": True,
    })
    assert confirmed.status_code == 200
    assert fake_si_client.create_grn.call_count == 2
    assert event_log("duplicate_submission")[-1]["action"] == "submitted_anyway"


def test_identical_payload_after_the_15_minute_window_is_not_a_duplicate(fake_si_client, monkeypatch, tmp_path):
    import time
    _catalog(monkeypatch, tmp_path)
    fake_si_client.create_grn.return_value = {"success": {}}
    now = time.time()
    clock = {"t": now}
    monkeypatch.setattr(server.time, "time", lambda: clock["t"])
    monkeypatch.setattr(server.grn_store.time, "time", lambda: clock["t"])

    assert _post_confirm([_item("A", 1)]).status_code == 200
    clock["t"] = now + 14 * 60
    assert _post_confirm([_item("A", 1)]).status_code == 409  # still inside the window
    clock["t"] = now + 16 * 60
    assert _post_confirm([_item("A", 1)]).status_code == 200  # a later, genuinely new delivery
    assert fake_si_client.create_grn.call_count == 2


def test_api_rejecting_one_line_names_that_line_and_why(fake_si_client, event_log, monkeypatch, tmp_path):
    from client import SalesIntellectError
    _catalog(monkeypatch, tmp_path)
    body = (
        '{"errors":{"code":"INVALID_VALUE","details":"GRN creation is restricted for products '
        'with stock control disabled","field":"items[2].product_id"}}'
    )
    fake_si_client.create_grn.side_effect = SalesIntellectError("POST", "/grn", 401, body)
    lines = [_item("Item A", 1), _item("Item B", 2), _item("Item C", 5), _item("Item D", 1)]

    results = _post_confirm(lines, headers={"X-Request-ID": "att-test04"}).get_json()["results"]

    assert len(results) == 4 and not any(r["success"] for r in results)
    first = results[0]  # the rejected line leads, with the API's reason
    assert first["name"] == "Item C"
    assert "GRN creation is restricted for products with stock control disabled" in first["error"]
    assert first["error"].endswith("Fix it in Sales Intellect, refresh catalog, then submit again.")
    others = {r["name"]: r["error"] for r in results[1:]}
    assert set(others) == {"Item A", "Item B", "Item D"}
    assert all("Item C was rejected" in e for e in others.values())
    (failed,) = event_log("grn_failed")
    assert failed["rejected_line"] == 2 and failed["rejected_name"] == "Item C"
    assert failed["status"] == 401 and failed["attempt_id"] == "att-test04"


def test_rejection_reason_is_read_from_details_detail_or_message(fake_si_client, monkeypatch, tmp_path):
    from client import SalesIntellectError
    _catalog(monkeypatch, tmp_path)
    for key in ("details", "detail", "message"):
        body = '{"errors":{"code":"INVALID_VALUE","%s":"Some specific reason","field":"items[0].product_id"}}' % key
        fake_si_client.create_grn.side_effect = SalesIntellectError("POST", "/grn", 401, body)
        first = _post_confirm([_item("Item A", 1), _item("Item B", 2)]).get_json()["results"][0]
        assert "Some specific reason" in first["error"], key
    # No explanation at all: fall back to the code rather than showing nothing.
    body = '{"errors":{"code":"INVALID_VALUE","field":"items[0].product_id"}}'
    fake_si_client.create_grn.side_effect = SalesIntellectError("POST", "/grn", 401, body)
    assert "INVALID_VALUE" in _post_confirm([_item("Item A", 1)]).get_json()["results"][0]["error"]


def test_other_api_failures_keep_the_generic_message(fake_si_client, monkeypatch, tmp_path):
    from client import SalesIntellectError
    _catalog(monkeypatch, tmp_path)
    for exc in (
        SalesIntellectError("POST", "/grn", 500, "<html>oops</html>"),
        SalesIntellectError("POST", "/grn", 401, '{"errors":{"code":"UNAUTHORIZED"}}'),
        SalesIntellectError("POST", "/grn", 401, '{"errors":{"field":"items[9].product_id","detail":"x"}}'),  # out of range
        RuntimeError("connection reset"),
    ):
        fake_si_client.create_grn.side_effect = exc
        errors = [r["error"] for r in _post_confirm([_item("A", 1), _item("B", 2)]).get_json()["results"]]
        assert all(e.startswith("Could not record goods received for") for e in errors)


def test_different_quantity_or_shop_is_not_a_duplicate(fake_si_client, monkeypatch, tmp_path):
    _catalog(monkeypatch, tmp_path)
    fake_si_client.create_grn.return_value = {"success": {}}

    assert _post_confirm([_item("A", 1)]).status_code == 200
    assert _post_confirm([_item("A", 2)]).status_code == 200  # different quantity
    other_shop = server.app.test_client().post("/confirm", json={
        "shop_id": "shop-2", "supplier_id": "sup-1", "items": [_item("A", 2)],
    })
    assert other_shop.status_code == 200  # same lines, different shop
    assert fake_si_client.create_grn.call_count == 3


def test_refused_or_failed_grn_does_not_become_the_previous_one(fake_si_client, monkeypatch, tmp_path):
    _catalog(monkeypatch, tmp_path)
    fake_si_client.create_grn.side_effect = [RuntimeError("API down"), {"success": {}}]

    assert _post_confirm([_item("A", 1)]).get_json()["results"][0]["success"] is False
    assert _post_confirm([_item("A", 1)]).status_code == 200  # retry after a failure is fine


def test_page_asks_before_resubmitting_identical_grn(grn_skip_review, event_log, monkeypatch):
    import requests
    from streamlit.testing.v1 import AppTest

    sent = []
    inner_post = requests.post

    def duplicate_aware_post(url, json=None, timeout=None, headers=None):
        if url.endswith("/confirm"):
            sent.append(json)
            if len(sent) == 2 and not json.get("confirm_duplicate"):
                return FakeResponse(409, {
                    "error": "These 2 line(s) are identical to the last GRN submitted for Branch A (35 s ago). Submit again?",
                    "duplicate": True, "previous": {"age_s": 35, "lines": 2, "attempt_id": "att-first"},
                })
        return inner_post(url, json=json, timeout=timeout, headers=headers)

    monkeypatch.setattr(requests, "post", duplicate_aware_post)
    grn_skip_review["parse_items"] = PARSE_CLEAN

    at = AppTest.from_file(str(APP_DIR / "app.py"))
    at.run()
    at.switch_page("pages/goods_received.py")
    at.run()
    _start(at)
    for _ in range(2):  # first submit succeeds, second is the identical resubmit
        at.text_area(key="grn_paste_input").set_value("anything").run()
        at.button(key="grn_submit_btn").click().run()
        assert not at.exception, at.exception

    assert len(sent) == 2
    assert any("identical to the last GRN" in w.value for w in at.warning)
    assert [b.key for b in at.button if b.key and "dup" in b.key] == ["grn_dup_again_btn", "grn_dup_cancel_btn"]
    assert event_log("duplicate_submission")[0]["age_s"] == 35

    at.button(key="grn_dup_again_btn").click().run()
    assert not at.exception, at.exception
    assert len(sent) == 3 and sent[2]["confirm_duplicate"] is True
    assert not [w for w in at.warning if "identical" in w.value]
    assert event_log("duplicate_decision")[0]["decision"] == "submit_anyway"


def test_cancelling_the_duplicate_warning_sends_nothing(grn_skip_review, event_log, monkeypatch):
    import requests
    from streamlit.testing.v1 import AppTest

    sent = []

    def always_duplicate(url, json=None, timeout=None, headers=None):
        if url.endswith("/parse"):
            return FakeResponse(200, {"items": PARSE_CLEAN})
        sent.append(json)
        return FakeResponse(409, {"error": "Same as last. Submit again?", "duplicate": True, "previous": {}})

    monkeypatch.setattr(requests, "post", always_duplicate)
    at = AppTest.from_file(str(APP_DIR / "app.py"))
    at.run()
    at.switch_page("pages/goods_received.py")
    at.run()
    _start(at)
    at.text_area(key="grn_paste_input").set_value("anything").run()
    at.button(key="grn_submit_btn").click().run()
    at.button(key="grn_dup_cancel_btn").click().run()

    assert len(sent) == 1
    assert at.text_area(key="grn_paste_input").value == "anything"  # their paste is still there
    assert not at.warning
    assert event_log("duplicate_decision")[0]["decision"] == "cancelled"


def test_page_logs_the_grn_number_from_its_existing_lookup(grn_skip_review, event_log, monkeypatch):
    from streamlit.testing.v1 import AppTest
    import client as client_module

    monkeypatch.setattr(
        client_module.SalesIntellectClient, "get_grn",
        lambda self, **kw: [{"grn_number": "GRN-10042", "items": [{"product_name": "Item A1", "grn_qty": 8}] * 2}],
    )
    grn_skip_review["parse_items"] = PARSE_CLEAN
    at = AppTest.from_file(str(APP_DIR / "app.py"))
    at.run()
    at.switch_page("pages/goods_received.py")
    at.run()
    _start(at)
    at.text_area(key="grn_paste_input").set_value("anything").run()
    at.button(key="grn_submit_btn").click().run()

    (found,) = event_log("grn_number_found")
    assert found["grn_number"] == "GRN-10042" and found["lines_found"] == found["lines_sent"] == 2
    assert found["attempt_id"] == event_log("submit_clicked")[0]["attempt_id"]
    assert [t.value for t in at.toast] == ["Saved GRN-10042 with 2 line(s)."]


def test_si_call_events_log_status_and_body_only_on_failure(event_log, monkeypatch):
    import client as client_module

    class Resp:
        def __init__(self, status, text):
            self.status_code, self.text, self.content = status, text, text.encode()
            self.headers = {}

        @property
        def ok(self):
            return self.status_code < 400

        def json(self):
            return {"ok": True}

    responses = iter([Resp(200, '{"ok": true}'), Resp(401, "x" * 500)])
    c = client_module.SalesIntellectClient(token="secret-token")
    monkeypatch.setattr(c.session, "request", lambda *a, **k: next(responses))
    events.bind(attempt_id="att-test03")

    c._request("GET", "/shops")
    with pytest.raises(Exception):
        c._request("POST", "/grn", json={})

    ok, failed = event_log("si_call")[0], event_log("si_call_failed")[0]
    assert ok["attempt_id"] == "att-test03" and ok["status"] == 200 and "body_head" not in ok
    assert failed["status"] == 401 and failed["level"] == "ERROR"
    assert len(failed["body_head"]) <= events.BODY_HEAD_CHARS + 1
    assert "secret-token" not in Path(events._LOG_PATH).read_text()
    events.clear()


def test_submit_logs_click_resume_and_one_attempt_id_end_to_end(grn_skip_review, event_log, monkeypatch):
    import requests
    from streamlit.testing.v1 import AppTest

    sent_headers = []
    inner_post = requests.post

    def spying_post(url, json=None, timeout=None, headers=None):
        sent_headers.append((url.rsplit("/", 1)[-1], (headers or {}).get("X-Request-ID")))
        return inner_post(url, json=json, timeout=timeout, headers=headers)

    monkeypatch.setattr(requests, "post", spying_post)
    grn_skip_review["parse_items"] = PARSE_CLEAN

    at = AppTest.from_file(str(APP_DIR / "app.py"))
    at.run()
    at.switch_page("pages/goods_received.py")
    at.run()
    _start(at)
    at.text_area(key="grn_paste_input").set_value("anything").run()
    at.button(key="grn_submit_btn").click().run()
    assert not at.exception, at.exception

    (start,) = event_log("session_start")
    (clicked,) = event_log("submit_clicked")
    (resumed,) = event_log("submit_resumed")
    attempt_id = clicked["attempt_id"]
    assert attempt_id.startswith("att-")
    assert clicked["source"] == "button_click" and clicked["session_id"] == start["session_id"]
    assert clicked["browser_id"] == start["browser_id"] and clicked["text_lines"] == 1
    assert resumed["source"] == "busy_resume" and resumed["attempt_id"] == attempt_id
    assert "note" not in resumed  # a click came first
    assert event_log("attempt_started")[0]["attempt_id"] == attempt_id
    assert event_log("busy_set")[0]["attempt_id"] == attempt_id
    assert event_log("busy_cleared")[0]["held_ms"] >= 0
    assert event_log("attempt_ended")[0]["outcome"] == "submitted"
    # The same ID went to Flask on /parse and /confirm as X-Request-ID.
    assert ("parse", attempt_id) in sent_headers and ("confirm", attempt_id) in sent_headers


def test_resume_without_a_click_is_logged_as_suspicious(grn_skip_review, event_log):
    from streamlit.testing.v1 import AppTest

    grn_skip_review["parse_items"] = PARSE_CLEAN
    at = AppTest.from_file(str(APP_DIR / "app.py"))
    at.run()
    at.switch_page("pages/goods_received.py")
    at.run()
    _start(at)
    # A busy flag appearing with no click — the case we could not rule out on 28 Oct.
    at.session_state["_busy__grn_submit"] = True
    at.session_state["_busy_payload__grn_submit__paste_text"] = "anything"
    at.run()

    (resumed,) = event_log("submit_resumed")
    assert resumed["level"] == "WARN"
    assert resumed["note"] == "no submit_clicked with this attempt_id"


def test_old_busy_flag_is_logged_as_stale(grn_skip_review, event_log):
    import time
    from streamlit.testing.v1 import AppTest

    grn_skip_review["parse_items"] = PARSE_CLEAN
    at = AppTest.from_file(str(APP_DIR / "app.py"))
    at.run()
    at.switch_page("pages/goods_received.py")
    at.run()
    _start(at)
    at.session_state["_busy__grn_submit"] = True
    at.session_state["_busy_since__grn_submit"] = time.time() - 94
    at.session_state["_busy_payload__grn_submit__paste_text"] = "anything"
    at.run()

    (stale,) = event_log("busy_stale")
    assert stale["level"] == "WARN" and stale["busy_age_s"] >= 94
    assert stale["action"] == "logged_only"


def test_missing_quantity_is_flagged_on_the_page_and_blocks_submit(grn_skip_review, event_log):
    from streamlit.testing.v1 import AppTest

    grn_skip_review["parse_items"] = PARSE_NO_QTY
    at = AppTest.from_file(str(APP_DIR / "app.py"))
    at.run()
    at.switch_page("pages/goods_received.py")
    at.run()
    _start(at)
    at.text_area(key="grn_paste_input").set_value("anything")
    at.button(key="grn_submit_btn").click().run()
    assert not at.exception, at.exception

    # Nothing sent; the exact-match line is shown anyway because it has no quantity.
    assert grn_skip_review["confirmed"] == []
    assert "1/2 item(s) are flagged" in at.session_state["grn_status_message"]
    qty_inputs = [n for n in at.number_input if n.key and n.key.startswith("grn_qty_")]
    assert len(qty_inputs) == 1 and qty_inputs[0].value is None
    assert any("No quantity" in c.value for c in at.caption)
    assert at.button(key="grn_submit_btn").disabled is True

    # Typing a quantity clears the flag; the full batch then goes through.
    qty_inputs[0].set_value(4).run()
    assert at.button(key="grn_submit_btn").disabled is False
    at.button(key="grn_submit_btn").click().run()
    assert not at.exception, at.exception
    (batch,) = grn_skip_review["confirmed"]
    assert [i["quantity"] for i in batch["items"]] == [5, 4]


def _page_showing_confirm_results(grn_skip_review, monkeypatch, results):
    """Runs a GRN submit whose /confirm response carries `results`, returns the AppTest."""
    import requests
    from streamlit.testing.v1 import AppTest

    inner_post = requests.post

    def post(url, json=None, timeout=None, headers=None):
        if url.endswith("/confirm"):
            return FakeResponse(200, {"results": results})
        return inner_post(url, json=json, timeout=timeout, headers=headers)

    monkeypatch.setattr(requests, "post", post)
    grn_skip_review["parse_items"] = PARSE_CLEAN
    at = AppTest.from_file(str(APP_DIR / "app.py"))
    at.run()
    at.switch_page("pages/goods_received.py")
    at.run()
    _start(at)
    at.text_area(key="grn_paste_input").set_value("anything").run()
    at.button(key="grn_submit_btn").click().run()
    assert not at.exception, at.exception
    return at


def _fail(name, cause=None, error="x"):
    row = {"raw_text": name, "name": name, "success": False, "error": error}
    if cause:
        row["cause"] = cause
    return row


def test_page_shows_only_the_rejected_line_and_one_summary(grn_skip_review, monkeypatch):
    results = [_fail("Item C", "rejected", "Sales Intellect rejected this line: Some reason.")] + [
        _fail(f"Item {n}", "blocked", "Not saved — nothing in this GRN was created because Item C was rejected.")
        for n in "ABDEFG"
    ]
    at = _page_showing_confirm_results(grn_skip_review, monkeypatch, results)

    assert [e.value for e in at.error] == ["✗ Item C — Sales Intellect rejected this line: Some reason."]
    assert [w.value for w in at.warning] == ["6 other line(s) were not saved. Nothing in this GRN was created."]
    assert not any("Item A" in m.value for m in at.markdown)


def test_page_shows_one_line_when_the_whole_call_failed(grn_skip_review, monkeypatch):
    results = [_fail(f"Item {n}", "batch", f"Could not record goods received for Item {n} at Branch A.") for n in "ABC"]
    at = _page_showing_confirm_results(grn_skip_review, monkeypatch, results)

    (error,) = at.error
    assert "Could not record this GRN — nothing was saved (3 line(s))." in error.value
    assert "Item A" not in error.value


def test_page_keeps_one_row_per_item_when_results_have_no_cause(grn_skip_review, monkeypatch):
    results = [{"raw_text": "Item A", "name": "Item A", "success": True}, _fail("Item B", error="No stock"), _fail("Item C", error="No stock")]
    at = _page_showing_confirm_results(grn_skip_review, monkeypatch, results)

    assert [e.value for e in at.error] == ["✗ Item B — No stock", "✗ Item C — No stock"]
    assert [s.value for s in at.success] == ["✓ Item A"]
