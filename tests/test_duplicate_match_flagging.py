"""Tests for _flag_duplicate_matches: when two or more lines resolve to the
SAME product, every one of them must be flagged (matched_product_id cleared,
with a flag_reason explaining why) instead of being silently merged into one
summed line or submitted as separate duplicate GRN lines.
"""

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import server  # noqa: E402


def _item(raw_text, matched_product_id, quantity=1, action="goods_received"):
    return {
        "raw_text": raw_text,
        "matched_product_id": matched_product_id,
        "candidates": [],
        "name": "Explorer Vest Navy 120" if matched_product_id else "",
        "quantity": quantity,
        "fields": {},
        "code": "EV120" if matched_product_id else None,
        "exact_match": False,
        "action": action,
    }


def test_no_collision_leaves_items_untouched():
    items = [_item("Explorer vest navy 120 1", "p1"), _item("Beach ball 2", "p2")]

    result = server._flag_duplicate_matches(items)

    assert result[0]["matched_product_id"] == "p1"
    assert result[1]["matched_product_id"] == "p2"
    assert "flag_reason" not in result[0]
    assert "flag_reason" not in result[1]


def test_collision_flags_every_line_including_the_first():
    items = [
        _item("Explorer vest green 120 1", "p1", quantity=1),
        _item("Explorer vest green 1200 2", "p1", quantity=2),
        _item("Explorer vest green 120 1", "p1", quantity=1),
    ]

    result = server._flag_duplicate_matches(items)

    # Every line in the collision is flagged -- not just the 2nd/3rd.
    assert all(it["matched_product_id"] is None for it in result)
    assert all(it.get("flag_reason") for it in result)
    # Quantities are untouched (no silent summing) -- staff see each
    # original line separately with its own quantity.
    assert [it["quantity"] for it in result] == [1, 2, 1]


def test_flag_reason_names_the_other_colliding_lines():
    items = [
        _item("Explorer vest green 120 1", "p1"),
        _item("Explorer vest green 1200 2", "p1"),
    ]

    result = server._flag_duplicate_matches(items)

    assert "Explorer vest green 1200 2" in result[0]["flag_reason"]
    assert "Explorer vest green 120 1" in result[1]["flag_reason"]


def test_unmatched_lines_are_not_treated_as_a_collision():
    """Two lines with matched_product_id=None are not "duplicates of each
    other" -- there's no match to collide on."""
    items = [_item("Unknown thing 1", None), _item("Unknown thing 2", None)]

    result = server._flag_duplicate_matches(items)

    assert result[0].get("flag_reason") is None
    assert result[1].get("flag_reason") is None


def test_collision_can_span_different_products_independently():
    """Two separate collisions in the same batch are each flagged on their
    own -- flagging one product's group doesn't touch an unrelated one."""
    items = [
        _item("Explorer vest green 120 1", "p1"),
        _item("Explorer vest green 1200 2", "p1"),
        _item("Beach ball red 1", "p2"),
        _item("Beach ball crimson 1", "p2"),
        _item("Sun hat 1", "p3"),
    ]

    result = server._flag_duplicate_matches(items)

    assert result[0]["matched_product_id"] is None
    assert result[1]["matched_product_id"] is None
    assert result[2]["matched_product_id"] is None
    assert result[3]["matched_product_id"] is None
    assert result[4]["matched_product_id"] == "p3"  # untouched, no collision


def test_parse_route_flags_two_exact_matches_to_the_same_product(monkeypatch, tmp_path):
    """End-to-end through POST /parse: two lines that both exact-match the
    same catalog product come back flagged, not merged, not duplicated."""
    monkeypatch.setattr(server, "si_client", object())
    monkeypatch.setattr(server, "CATALOG_CACHE_PATH", str(tmp_path / "catalog_cache.json"))
    catalog = [{"product_id": "p1", "name": "Explorer Vest Navy 120", "code": "EV120", "cost": 0}]
    monkeypatch.setattr(server, "_catalog_state", {
        "products": catalog,
        "fetched_at": "2026-01-01T00:00:00",
        "index": server.CatalogIndex(catalog),
        "cache_mtime": None,
    })

    client = server.app.test_client()
    resp = client.post("/parse", json={
        "text": "Explorer vest navy 120 1\nExplorer vest navy 120 2",
        "mode": "goods_received",
    })

    assert resp.status_code == 200
    items = resp.get_json()["items"]
    assert len(items) == 2
    assert all(it["matched_product_id"] is None for it in items)
    assert all(it.get("flag_reason") for it in items)
