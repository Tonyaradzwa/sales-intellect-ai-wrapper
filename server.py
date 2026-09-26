"""Flask backend for parsing pasted text into structured line items via an agent.

Exposes:
    POST /parse    -> turn raw pasted text into new line items, using the Claude
                      Agent SDK (search_products / generate_next_product_code /
                      submit_result tools) instead of a single big JSON-in JSON-out
                      prompt
    POST /confirm  -> write the (possibly staff-edited) line items to Sales Intellect

Run with:
    export SI_API_TOKEN=your_si_token
    export ANTHROPIC_API_KEY=your_anthropic_key
    python server.py

Requires the Claude Code CLI on PATH (the Agent SDK drives it as a subprocess):
    npm install -g @anthropic-ai/claude-code
"""

import asyncio
import difflib
import json
import os
import re
from datetime import date

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, create_sdk_mcp_server, query, tool
from flask import Flask, jsonify, request

from client import SalesIntellectClient

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
SEARCH_RESULT_LIMIT = 10

app = Flask(__name__)

try:
    si_client = SalesIntellectClient()
except ValueError as e:
    si_client = None
    si_client_error = str(e)


STOCK_UPDATE_AGENT_PROMPT = """You are a data-entry assistant helping staff record \
stock received against EXISTING products in a point-of-sale catalog. You will be \
given raw, informal text (often copy-pasted from WhatsApp) describing stock \
received for one or more existing products. Every item you produce is a \
stock update against a product that already exists — you never create new \
products here.

For each distinct product mentioned in the text:
1. Call normalize_stock_line on the raw phrase FIRST, before searching. Product
   descriptions often end in a trailing number that is the quantity received —
   not part of the name — even when the description itself also contains a
   number earlier on (e.g. a size, like "120" in "Leotard Black 120 3": the "3"
   at the end is the quantity, "120" is part of the product description). This
   tool splits that reliably; use its returned name_query (not the raw phrase)
   as your search_products query, and use its returned quantity unless the
   message clearly states the quantity some other way.
2. Call search_products with that name_query to find candidate matches.
   search_products does tokenized matching (word order and extra leftover
   terms don't block a match), so a missing word like "Size" in the input
   (e.g. "Leotard Black 120" vs. the catalog's "Leotard Black Size 120") is
   still a valid match; don't require exact wording. If the first search
   doesn't return a strong, confident match, retry with progressively
   broader queries and merge the candidates from every attempt before
   deciding: (a) the full name_query, (b) the same query with a trailing
   number/size term dropped (e.g. "leotard black 15" -> "leotard black"),
   then (c) just the core noun/category (e.g. "leotard black" -> "leotard").
   Stop once you have a confident match or you've tried the core noun.
3. If the text references a GROUP of products rather than one specific item (e.g.
   "all pink leotards", "all the Sbart ones", "all Extra Large"), search and
   identify EVERY matching product, then call submit_result once per matching
   product — all of them sharing the same raw_text phrase and the same quantity.
4. If you're confident of a single match, call submit_result with that product's
   id as matched_product_id.
5. If NO product reasonably matches, still call submit_result with
   matched_product_id set to null, so staff can be told it wasn't found. Do not
   skip the item and do not guess a wrong match just to fill the field.
6. Ignore filler lines that aren't product mentions at all (e.g. "Goods received").

Call submit_result exactly once for every line item / matching product you
process. Once you've handled the entire text, stop — don't produce any other
output.
"""

GOODS_RECEIVED_AGENT_PROMPT = """You are a data-entry assistant helping staff record \
a goods received note (GRN) — physical stock received from a supplier delivery — \
against EXISTING products in a point-of-sale catalog. You will be given raw, \
informal text (often copy-pasted from WhatsApp) describing stock received. Every \
item you produce is against a product that already exists — you never create new \
products here. Every quantity here is stock being ADDED (goods physically \
received) — never a negative adjustment.

For each distinct product mentioned in the text:
1. Call normalize_stock_line on the raw phrase FIRST, before searching. Product
   descriptions often end in a trailing number that is the quantity received —
   not part of the name — even when the description itself also contains a
   number earlier on (e.g. a size, like "120" in "Leotard Black 120 3": the "3"
   at the end is the quantity, "120" is part of the product description). This
   tool splits that reliably; use its returned name_query (not the raw phrase)
   as your search_products query, and use its returned quantity unless the
   message clearly states the quantity some other way.
2. Call search_products with that name_query to find candidate matches.
   search_products does tokenized matching (word order and extra leftover
   terms don't block a match), so a missing word like "Size" in the input
   (e.g. "Leotard Black 120" vs. the catalog's "Leotard Black Size 120") is
   still a valid match; don't require exact wording. If the first search
   doesn't return a strong, confident match, retry with progressively
   broader queries and merge the candidates from every attempt before
   deciding: (a) the full name_query, (b) the same query with a trailing
   number/size term dropped (e.g. "leotard black 15" -> "leotard black"),
   then (c) just the core noun/category (e.g. "leotard black" -> "leotard").
   Stop once you have a confident match or you've tried the core noun.
3. If the text references a GROUP of products rather than one specific item (e.g.
   "all pink leotards", "all the Sbart ones", "all Extra Large"), search and
   identify EVERY matching product, then call submit_result once per matching
   product — all of them sharing the same raw_text phrase and the same quantity.
4. If you're confident of a single match, call submit_result with that product's
   id as matched_product_id.
5. If NO product reasonably matches, still call submit_result with
   matched_product_id set to null, so staff can be told it wasn't found. Do not
   skip the item and do not guess a wrong match just to fill the field.
6. Ignore filler lines that aren't product mentions at all (e.g. "Goods received",
   a delivery date, or a supplier name on its own line).

Call submit_result exactly once for every line item / matching product you
process. Once you've handled the entire text, stop — don't produce any other
output.
"""

NEW_PRODUCT_AGENT_PROMPT = """You are a data-entry assistant helping staff add \
brand new products to a point-of-sale catalog. You will be given raw, informal \
text (often copy-pasted from WhatsApp) listing brand new products to add. Every \
item you produce is a new product — never a stock update.

For each distinct product mentioned in the text:
1. Call search_products using the product's category/type (e.g. "kickboards",
   "goggles") to find similarly-named EXISTING products — purely so you can match
   their naming STYLE (word order, delimiters, capitalization pattern), not to
   match against them as the same product.
2. Compose a name for the new product: capitalize every word (Title Case), and
   always spell out "With" in full instead of abbreviations like "w/" — even if
   similar existing products use "w/". Otherwise follow the style of similar
   existing products where a clear pattern exists.
3. Call generate_next_product_code to get a suggested product code for this
   product. Call it once per new product (it returns a different code each time).
4. Extract any explicitly stated attributes (e.g. price) and initial stock
   quantity, if mentioned.
5. Call submit_result with the composed name, the generated code, the quantity,
   and the fields.

Call submit_result exactly once for every new product mentioned. Once you've
handled the entire text, stop — don't produce any other output.
"""

STOCK_UPDATE_SUBMIT_SCHEMA = {
    "type": "object",
    "properties": {
        "raw_text": {
            "type": "string",
            "description": "Exact substring of the input this item was derived from.",
        },
        "matched_product_id": {
            "type": ["string", "null"],
            "description": "The matched product's id, or null if no confident match was found.",
        },
        "quantity": {
            "type": ["integer", "null"],
            "description": "Quantity delta to add to current stock, or null if not stated.",
        },
    },
    "required": ["raw_text", "matched_product_id", "quantity"],
}

GOODS_RECEIVED_SUBMIT_SCHEMA = {
    "type": "object",
    "properties": {
        "raw_text": {
            "type": "string",
            "description": "Exact substring of the input this item was derived from.",
        },
        "matched_product_id": {
            "type": ["string", "null"],
            "description": "The matched product's id, or null if no confident match was found.",
        },
        "quantity": {
            "type": ["integer", "null"],
            "description": "Quantity received (always added to stock), or null if not stated.",
        },
    },
    "required": ["raw_text", "matched_product_id", "quantity"],
}

NEW_PRODUCT_SUBMIT_SCHEMA = {
    "type": "object",
    "properties": {
        "raw_text": {
            "type": "string",
            "description": "Exact substring of the input this item was derived from.",
        },
        "suggested_name": {
            "type": "string",
            "description": "Formatted product name (Title Case, 'With' spelled out).",
        },
        "suggested_product_code": {
            "type": ["string", "null"],
            "description": "Code returned by generate_next_product_code, or null.",
        },
        "quantity": {
            "type": ["integer", "null"],
            "description": "Initial stock quantity if stated, else null.",
        },
        "fields": {
            "type": "object",
            "properties": {"price": {"type": ["number", "null"]}},
            "description": "Other explicitly stated attributes, e.g. price.",
        },
    },
    "required": ["raw_text", "suggested_name", "suggested_product_code", "quantity", "fields"],
}


def _trimmed_catalog(products):
    if isinstance(products, dict):
        for value in products.values():
            if isinstance(value, list):
                products = value
                break
    catalog = []
    for p in products:
        catalog.append({
            "product_id": p.get("id"),
            "name": p.get("product_name"),
            "code": p.get("product_code"),
        })
    return catalog


_TRAILING_QTY_RE = re.compile(r"^(?P<name>.*\S)\s+(?P<sign>[+-]?)[xX]?(?P<qty>\d+)\s*$")


def _split_trailing_quantity(raw_text):
    """Split "<product description> <qty>" into (name_query, quantity).

    The quantity is virtually always the LAST number in the phrase, even when
    the description itself contains an earlier number (e.g. a size, as in
    "Leotard Black 120 3" -> name "Leotard Black 120", quantity 3). Handles a
    trailing sign ("+5") and an "x" prefix ("x12"). Returns quantity None if
    the phrase doesn't end in a number at all.
    """
    text = raw_text.strip()
    m = _TRAILING_QTY_RE.match(text)
    if not m:
        return {"name_query": text, "quantity": None}
    qty = int(m.group("qty"))
    if m.group("sign") == "-":
        qty = -qty
    return {"name_query": m.group("name").strip(), "quantity": qty}


_CODE_RE = re.compile(r"^(?P<prefix>\D*)(?P<num>\d+)$")


def _suggest_next_code(existing_codes):
    """Given existing product codes, suggest the next one in sequence.

    Finds the code with the highest numeric value (matching an optional
    non-digit prefix followed by digits), and increments it by one,
    preserving the prefix and zero-padded width. Returns None if no code
    matches that pattern.
    """
    matches = []
    for code in existing_codes:
        if not code:
            continue
        m = _CODE_RE.match(str(code))
        if m:
            matches.append((m.group("prefix"), m.group("num")))

    if not matches:
        return None

    prefix, num_str = max(matches, key=lambda pn: int(pn[1]))
    next_num = int(num_str) + 1
    return f"{prefix}{str(next_num).zfill(len(num_str))}"


def _normalize_new_product_name(name):
    """Title-case every word and spell out "With" instead of "w/"."""
    if not name:
        return name
    name = re.sub(r"(?i)w/\s*", "With ", name)
    return " ".join(w[0].upper() + w[1:] if w else w for w in name.split())


def _assign_codes(items, catalog, reserve_existing_item_codes=False):
    """Normalize new_product names and give any without a code a fresh one."""
    used_codes = {c["code"] for c in catalog if c.get("code")}
    if reserve_existing_item_codes:
        for item in items:
            if item.get("code"):
                used_codes.add(item["code"])

    for item in items:
        if item["action"] == "new_product":
            item["name"] = _normalize_new_product_name(item.get("name"))
            if not item.get("code"):
                suggested = _suggest_next_code(used_codes)
                item["code"] = suggested
                if suggested:
                    used_codes.add(suggested)

    return items


# Spelled-out size words get folded to the same short token as their letter
# abbreviation (order matters: longer phrases first so "extra large" doesn't
# get left as "extra l" after the "large" pattern already ran).
_SIZE_CANON_PATTERNS = [
    (re.compile(r"\bextra\s*extra\s*large\b"), "xxl"),
    (re.compile(r"\bextra\s*large\b"), "xl"),
    (re.compile(r"\bextra\s*small\b"), "xs"),
    (re.compile(r"\bsmall\b"), "s"),
    (re.compile(r"\bmedium\b"), "m"),
    (re.compile(r"\blarge\b"), "l"),
]

# Filler words that appear around a size in some catalog names ("... Size
# 120") but not necessarily in how staff describe the same item ("... 120").
_SEARCH_STOPWORDS = {"size", "sz"}


def _stem(word):
    """Naive plural stripping so "pinks" matches a catalog word "pink".

    Only strips a trailing "s" on words long enough that it's very unlikely
    to be part of the word itself (not a plural), and never touches words
    ending "ss" (e.g. "dress").
    """
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _search_words(text):
    """Lowercase, canonicalize size words, and split into stemmed match tokens."""
    text = (text or "").lower()
    for pattern, canon in _SIZE_CANON_PATTERNS:
        text = pattern.sub(canon, text)
    words = re.findall(r"[a-z0-9]+", text)
    return [_stem(w) for w in words if w not in _SEARCH_STOPWORDS]


def _search_catalog(catalog, query_text, limit=SEARCH_RESULT_LIMIT):
    """Dependency-free tokenized search over {"product_id","name","code"} dicts.

    Ranks by how many normalized query words appear in the product name, so
    word order and extra terms (like a leftover size/number word the query
    wasn't fully stripped of) don't prevent a match the way literal substring
    matching would. Ties are broken by preferring shorter names (fewer
    unmatched extra words) and then by overall string similarity.
    """
    query_words = _search_words(query_text)
    if not query_words:
        return []

    scored = []
    for p in catalog:
        name = p.get("name") or ""
        if not name:
            continue
        name_words = _search_words(name)
        match_count = sum(1 for w in query_words if w in name_words)
        if match_count == 0:
            continue
        ratio = difflib.SequenceMatcher(None, " ".join(query_words), " ".join(name_words)).ratio()
        scored.append((match_count, -len(name_words), ratio, p))

    scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    return [{"product_id": p["product_id"], "name": p["name"]} for _, _, _, p in scored[:limit]]


def _run_parse_agent(mode, text, catalog):
    """Runs the Claude Agent SDK's built-in tool-call loop to turn `text` into items.

    Tools are scoped per request: search_products (both modes),
    generate_next_product_code (new_products only), and submit_result (schema
    depends on mode). The agent calls submit_result once per line item; each
    call is captured here and translated into our canonical item schema.
    """
    collected_items = []
    used_codes = {c["code"] for c in catalog if c.get("code")}

    @tool(
        "search_products",
        "Fuzzy/substring search the product catalog by name. Returns up to "
        f"{SEARCH_RESULT_LIMIT} candidate matches as a JSON list of "
        '{"product_id", "name"}.',
        {"query": str},
    )
    async def search_products(args):
        results = _search_catalog(catalog, args.get("query", ""))
        text_out = json.dumps(results) if results else "No matching products found."
        return {"content": [{"type": "text", "text": text_out}]}

    tools = [search_products]

    if mode == "new_products":
        @tool(
            "generate_next_product_code",
            "Generate the next unused product code, following the numbering "
            "convention already used in the catalog. Call once per new product.",
            {},
        )
        async def generate_next_product_code(args):
            code = _suggest_next_code(used_codes)
            if code:
                used_codes.add(code)
            text_out = code or "No numeric code pattern found in the catalog; use null."
            return {"content": [{"type": "text", "text": text_out}]}

        tools.append(generate_next_product_code)

        @tool(
            "submit_result",
            "Submit one finished new-product line item. Call once per new "
            "product, after composing its name and generating its code.",
            NEW_PRODUCT_SUBMIT_SCHEMA,
        )
        async def submit_result(args):
            collected_items.append({
                "action": "new_product",
                "raw_text": args["raw_text"],
                "matched_product_id": None,
                "candidates": [],
                "name": args.get("suggested_name", ""),
                "quantity": args.get("quantity"),
                "fields": args.get("fields") or {},
                "code": args.get("suggested_product_code"),
            })
            return {"content": [{"type": "text", "text": "Recorded."}]}

        tools.append(submit_result)
        system_prompt = NEW_PRODUCT_AGENT_PROMPT
    elif mode == "goods_received":
        @tool(
            "normalize_stock_line",
            "Split a raw product-mention phrase into a clean search query and "
            "its trailing quantity. The last number in the phrase is treated "
            "as the quantity, even if the description contains an earlier "
            "number (e.g. a size). Call this before search_products.",
            {"raw_text": str},
        )
        async def normalize_stock_line(args):
            result = _split_trailing_quantity(args.get("raw_text", ""))
            return {"content": [{"type": "text", "text": json.dumps(result)}]}

        tools.append(normalize_stock_line)

        @tool(
            "submit_result",
            "Submit one finished goods-received line item. Call once per line "
            "item (or once per product, for a group reference) after "
            "searching and confirming a match, or determining none exists.",
            GOODS_RECEIVED_SUBMIT_SCHEMA,
        )
        async def submit_result(args):
            matched_id = args.get("matched_product_id")
            matched = next((p for p in catalog if p["product_id"] == matched_id), None)
            collected_items.append({
                "action": "goods_received",
                "raw_text": args["raw_text"],
                "matched_product_id": matched_id,
                "candidates": _search_catalog(catalog, args["raw_text"]),
                "name": matched["name"] if matched else "",
                "quantity": args.get("quantity"),
                "fields": {},
                "code": matched["code"] if matched else None,
            })
            return {"content": [{"type": "text", "text": "Recorded."}]}

        tools.append(submit_result)
        system_prompt = GOODS_RECEIVED_AGENT_PROMPT
    else:
        @tool(
            "normalize_stock_line",
            "Split a raw product-mention phrase into a clean search query and "
            "its trailing quantity. The last number in the phrase is treated "
            "as the quantity, even if the description contains an earlier "
            "number (e.g. a size). Call this before search_products.",
            {"raw_text": str},
        )
        async def normalize_stock_line(args):
            result = _split_trailing_quantity(args.get("raw_text", ""))
            return {"content": [{"type": "text", "text": json.dumps(result)}]}

        tools.append(normalize_stock_line)

        @tool(
            "submit_result",
            "Submit one finished stock-update line item. Call once per line "
            "item (or once per product, for a group reference) after "
            "searching and confirming a match, or determining none exists.",
            STOCK_UPDATE_SUBMIT_SCHEMA,
        )
        async def submit_result(args):
            matched_id = args.get("matched_product_id")
            matched = next((p for p in catalog if p["product_id"] == matched_id), None)
            collected_items.append({
                "action": "stock_update",
                "raw_text": args["raw_text"],
                "matched_product_id": matched_id,
                "candidates": _search_catalog(catalog, args["raw_text"]),
                "name": matched["name"] if matched else "",
                "quantity": args.get("quantity"),
                "fields": {},
                "code": matched["code"] if matched else None,
            })
            return {"content": [{"type": "text", "text": "Recorded."}]}

        tools.append(submit_result)
        system_prompt = STOCK_UPDATE_AGENT_PROMPT

    server = create_sdk_mcp_server(name="pos", tools=tools)
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        mcp_servers={"pos": server},
        tools=[],  # no built-in tools (Bash, Read, etc.) — only our own
        permission_mode="bypassPermissions",
        model=MODEL,
    )

    async def _run():
        async for message in query(prompt=text, options=options):
            if isinstance(message, ResultMessage) and message.is_error:
                raise Exception(message.result or "Agent run ended in an error state.")

    asyncio.run(_run())
    return collected_items


@app.route("/parse", methods=["POST"])
def parse():
    if si_client is None:
        return jsonify({"error": si_client_error}), 500

    body = request.get_json(force=True, silent=True) or {}
    text = (body.get("text") or "").strip()
    mode = body.get("mode", "product_updates")

    if mode not in ("product_updates", "new_products", "goods_received"):
        return jsonify({"error": f"Invalid mode: {mode!r}"}), 400
    if not text:
        return jsonify({"error": "No text provided."}), 400

    # Always fetch fresh — no caching — and only once per request; search_products
    # and generate_next_product_code both search this same in-memory snapshot
    # rather than each hitting the API again.
    try:
        products = si_client.list_products()
    except Exception as e:
        return jsonify({"error": f"Failed to fetch product catalog: {e}"}), 502

    catalog = _trimmed_catalog(products)

    try:
        items = _run_parse_agent(mode, text, catalog)
    except Exception as e:
        return jsonify({"error": f"Agent run failed: {e}"}), 502

    items = _assign_codes(items, catalog, reserve_existing_item_codes=True)
    return jsonify({"items": items})


@app.route("/confirm", methods=["POST"])
def confirm():
    if si_client is None:
        return jsonify({"error": si_client_error}), 500

    body = request.get_json(force=True, silent=True) or {}
    shop_id = body.get("shop_id")
    supplier_id = body.get("supplier_id")
    items = body.get("items")

    if not isinstance(items, list) or not items:
        return jsonify({"error": "items must be a non-empty list."}), 400
    if not shop_id and any(item.get("action") in ("stock_update", "goods_received") for item in items):
        return jsonify({"error": "shop_id is required for stock updates and goods received."}), 400
    if not supplier_id and any(item.get("action") == "goods_received" for item in items):
        return jsonify({"error": "supplier_id is required for goods received."}), 400

    shop_name = None
    if shop_id:
        try:
            shops_resp = si_client.list_shops()
            shop_list = shops_resp.get("shops", []) if isinstance(shops_resp, dict) else shops_resp
            shop_name = next((s.get("shop_name") for s in shop_list if s.get("id") == shop_id), None)
        except Exception:
            pass  # Falls back to "this shop" in error messages below; not worth failing the whole confirm over.

    results = []
    grn_items = [item for item in items if item.get("action") == "goods_received"]
    other_items = [item for item in items if item.get("action") != "goods_received"]

    for item in other_items:
        action = item.get("action")
        raw_text = item.get("raw_text", "")
        name = item.get("name") or raw_text
        try:
            if action == "new_product":
                # Re-fetch fresh right before writing, in case another
                # submission took this code (or a lower one) in the meantime.
                fresh_catalog = _trimmed_catalog(si_client.list_products())
                fresh_codes = {c["code"] for c in fresh_catalog if c.get("code")}

                code = item.get("code")
                if not code or code in fresh_codes:
                    code = _suggest_next_code(fresh_codes)

                payload = {
                    "product_name": _normalize_new_product_name(item.get("name")),
                    # The API 400s if these booleans are omitted entirely (they
                    # come back null and fail its "must be a boolean" check) —
                    # these are reasonable defaults for a simple, non-variant,
                    # stock-tracked product.
                    "stock_control": True,
                    "product_price_change": True,
                    "qty_change_option": True,
                    "expire_mode": False,
                    "is_composite": False,
                    "use_production": False,
                    "is_variant": False,
                }
                if code:
                    payload["product_code"] = code
                price = (item.get("fields") or {}).get("price")
                if price is not None:
                    payload["cost"] = price
                result = si_client.upsert_product(payload)
            elif action == "stock_update":
                product_id = item.get("matched_product_id")
                quantity = item.get("quantity")
                if not product_id:
                    raise Exception("No matched_product_id set for stock update.")
                if quantity is None:
                    raise Exception("No quantity set for stock update.")
                result = si_client.adjust_inventory(shop_id, product_id, int(quantity))
            else:
                raise Exception(f"Unknown action: {action!r}")

            results.append({
                "raw_text": raw_text,
                "name": name,
                "success": True,
                "result": result,
            })
        except Exception as e:
            # Full detail (product_id, shop_id, API response) goes to the
            # server log only — staff shouldn't see internal ids.
            app.logger.exception(f"Confirm failed for action={action!r} raw_text={raw_text!r}")
            if action == "stock_update":
                error = f"Could not update inventory for {name} at {shop_name or 'this shop'}."
            else:
                error = str(e)
            results.append({
                "raw_text": raw_text,
                "name": name,
                "success": False,
                "error": error,
            })

    if grn_items:
        results.extend(_submit_grn(grn_items, shop_id, supplier_id, shop_name))

    return jsonify({"results": results})


# Confirmed against the real API as the correct value ("DIRECT GRN" — the
# string shown in GET /grn responses — is only a display label; POST
# rejects it and expects this instead).
_GRN_TYPE = "DIRECT"

# Only "Cash" is currently supported for goods-received payment method. No
# endpoint was found to discover other payment methods (no /payment-methods,
# and GET /grn doesn't expose one either without existing history), and this
# id worked unchanged across two different Sales Intellect accounts we
# tested against, suggesting it's shared/global rather than account-specific
# — but that's not confirmed against vendor docs. If another payment method
# is ever needed (e.g. "Card"), this needs a real discovery mechanism first.
_GRN_CASH_PAYMENT_METHOD_ID = "TldVZ1lzSnVUejBPRjN5RVVkWnAxdz09"


def _submit_grn(grn_items, shop_id, supplier_id, shop_name):
    """Create one GRN covering every goods_received item, mirroring how a
    real goods-received note bundles everything from one delivery together
    (rather than one API call per line item, like stock_update does).
    """
    results = []
    incomplete = [it for it in grn_items if not it.get("matched_product_id") or it.get("quantity") is None]
    complete = [it for it in grn_items if it not in incomplete]

    for item in incomplete:
        name = item.get("name") or item.get("raw_text", "")
        results.append({
            "raw_text": item.get("raw_text", ""),
            "name": name,
            "success": False,
            "error": f"{name}: no matched product or quantity — fix in the table before submitting.",
        })

    if not complete:
        return results

    # Cost defaults come from each product's own catalog cost, fetched fresh
    # (not the trimmed search catalog, which drops the cost field).
    cost_by_id = {}
    try:
        cost_by_id = {p.get("id"): p.get("cost") or 0 for p in si_client.list_products()}
    except Exception:
        app.logger.exception("Could not fetch product costs for GRN; defaulting unit_cost to 0.")

    grn_line_items = []
    grn_total = 0
    for item in complete:
        product_id = item["matched_product_id"]
        qty = int(item["quantity"])
        unit_cost = cost_by_id.get(product_id) or 0
        total_cost = unit_cost * qty
        grn_total += total_cost
        grn_line_items.append({
            "product_id": product_id,
            "qty": qty,
            "unit_cost": unit_cost,
            "total_unit_cost": total_cost,
            "expire_mode": "OFF",
            "expire_date": "",
        })

    payload = {
        "supplier_id": supplier_id,
        "shop_id": shop_id,
        "grn_date": date.today().isoformat(),
        "payment_method_id": _GRN_CASH_PAYMENT_METHOD_ID,
        "supplier_invoice_no": "",
        "addition_information": "",
        "grn_type": _GRN_TYPE,
        "grn_total": grn_total,
        "items": grn_line_items,
    }

    try:
        grn_result = si_client.create_grn(payload)
        for item in complete:
            results.append({
                "raw_text": item.get("raw_text", ""),
                "name": item.get("name") or item.get("raw_text", ""),
                "success": True,
                "result": grn_result,
            })
    except Exception:
        app.logger.exception(f"GRN creation failed for {len(complete)} item(s)")
        for item in complete:
            name = item.get("name") or item.get("raw_text", "")
            results.append({
                "raw_text": item.get("raw_text", ""),
                "name": name,
                "success": False,
                "error": f"Could not record goods received for {name} at {shop_name or 'this shop'}.",
            })

    return results


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_PORT", "5050"))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host=host, port=port, debug=debug)
