"""Tests for the catalog-index / exact-match short-circuit in server.py.

Covers: exact-match hits (including case/whitespace differences) never reach
the LLM, a duplicate catalog name is not auto-accepted, and a genuinely
non-exact name still goes through the fuzzy/LLM path.
"""

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import server  # noqa: E402


def _catalog(*names_and_codes):
    return [
        {"product_id": f"p{i}", "name": name, "code": code}
        for i, (name, code) in enumerate(names_and_codes, start=1)
    ]


def test_find_exact_matches_ignores_case_and_spacing():
    catalog = _catalog(("Cap Large Black", "C001"), ("Cap Large Blue", "C002"))
    index = server.CatalogIndex(catalog)

    exact_items, remaining = server._find_exact_matches(index, ["Cap  large   black 5"])

    assert remaining == []
    assert len(exact_items) == 1
    item = exact_items[0]
    assert item["matched_product_id"] == "p1"
    assert item["quantity"] == 5
    assert item["exact_match"] is True


def test_find_exact_matches_duplicate_name_is_not_auto_accepted():
    catalog = _catalog(("Cap Large Black", "C001"), ("Cap Large Black", "C002"))
    index = server.CatalogIndex(catalog)

    exact_items, remaining = server._find_exact_matches(index, ["Cap large black 5"])

    assert exact_items == []
    assert remaining == ["Cap large black 5"]


def test_find_exact_matches_no_match_falls_through():
    catalog = _catalog(("Widget Blue Deluxe", "B001"))
    index = server.CatalogIndex(catalog)

    exact_items, remaining = server._find_exact_matches(index, ["Widget Deluxe 2"])

    assert exact_items == []
    assert remaining == ["Widget Deluxe 2"]


def test_search_catalog_still_fuzzy_matches_via_index():
    catalog = _catalog(("Widget Blue Deluxe", "B001"), ("Widget Red Handle", "B002"))
    index = server.CatalogIndex(catalog)

    results = server._search_catalog(index, "Widget Deluxe")

    assert any(r["product_id"] == "p1" for r in results)


async def _empty_async_gen(prompt, options):
    return
    yield  # pragma: no cover - makes this an async generator that yields nothing


def test_exact_match_skips_llm_entirely(monkeypatch):
    catalog = _catalog(("Cap Large Black", "C001"))
    index = server.CatalogIndex(catalog)

    calls = {"count": 0}

    async def fake_query(prompt, options):
        calls["count"] += 1
        return
        yield  # pragma: no cover

    monkeypatch.setattr(server, "query", fake_query)

    items = server._run_parse_agent("goods_received", "Cap large black 5", index)

    assert calls["count"] == 0
    assert len(items) == 1
    assert items[0]["matched_product_id"] == "p1"
    assert items[0]["exact_match"] is True
    assert items[0]["quantity"] == 5


def test_duplicate_name_still_goes_through_llm(monkeypatch):
    catalog = _catalog(("Cap Large Black", "C001"), ("Cap Large Black", "C002"))
    index = server.CatalogIndex(catalog)

    calls = {"count": 0}

    async def fake_query(prompt, options):
        calls["count"] += 1
        return
        yield  # pragma: no cover

    monkeypatch.setattr(server, "query", fake_query)

    server._run_parse_agent("goods_received", "Cap large black 5", index)

    assert calls["count"] == 1


def test_non_exact_name_goes_through_llm(monkeypatch):
    catalog = _catalog(("Widget Blue Deluxe", "B001"))
    index = server.CatalogIndex(catalog)

    calls = {"count": 0}

    async def fake_query(prompt, options):
        calls["count"] += 1
        return
        yield  # pragma: no cover

    monkeypatch.setattr(server, "query", fake_query)

    items = server._run_parse_agent("goods_received", "Widget Deluxe 2", index)

    assert calls["count"] == 1
    # Our fake query never calls submit_result, so nothing was collected from
    # the LLM path in this test — the point is only that the LLM WAS invoked.
    assert items == []


def test_mixed_paste_only_sends_non_exact_lines_to_llm(monkeypatch):
    catalog = _catalog(("Cap Large Black", "C001"), ("Widget Blue Deluxe", "B001"))
    index = server.CatalogIndex(catalog)

    seen_prompts = []

    async def fake_query(prompt, options):
        seen_prompts.append(prompt)
        return
        yield  # pragma: no cover

    monkeypatch.setattr(server, "query", fake_query)

    items = server._run_parse_agent(
        "goods_received", "Cap large black 5\nWidget Deluxe 2", index
    )

    assert len(seen_prompts) == 1
    assert "Cap large black" not in seen_prompts[0]
    assert "Widget Deluxe" in seen_prompts[0]
    assert len(items) == 1
    assert items[0]["exact_match"] is True
    assert items[0]["matched_product_id"] == "p1"
