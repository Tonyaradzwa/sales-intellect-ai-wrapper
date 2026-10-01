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
import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
import time
from datetime import date, datetime

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, create_sdk_mcp_server, query, tool
from flask import Flask, jsonify, request

import timing
from client import SalesIntellectClient
from lfu_cache import LFUCache

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
SEARCH_RESULT_LIMIT = 10
DEPLOY_WEBHOOK_SECRET = os.environ.get("DEPLOY_WEBHOOK_SECRET")
DEPLOY_TRIGGER_SCRIPT = os.environ.get(
    "DEPLOY_TRIGGER_SCRIPT", "/home/ec2-user/app/deploy/trigger_deploy.sh"
)

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


def _normalize_exact(name):
    """Lowercase, trim, and collapse internal whitespace for exact-name
    lookups. Deliberately simpler than _search_words() — no stemming or
    size-canonicalization — so it only matches names that are really the
    same string modulo case and spacing.
    """
    return re.sub(r"\s+", " ", (name or "").strip().lower())


class CatalogIndex:
    """Built once per catalog (per /parse request) and reused for every
    item, instead of every matching call re-scanning and re-normalizing the
    raw catalog list from scratch.
    """

    def __init__(self, catalog):
        self.catalog = catalog
        self.by_id = {}
        self.by_exact_name = {}
        self.tokens_by_id = {}
        self.inverted = {}

        for p in catalog:
            pid = p.get("product_id")
            self.by_id[pid] = p

            name = p.get("name") or ""
            if not name:
                continue

            self.by_exact_name.setdefault(_normalize_exact(name), []).append(p)

            tokens = _search_words(name)
            self.tokens_by_id[pid] = tokens
            for word in tokens:
                self.inverted.setdefault(word, set()).add(pid)


def _search_catalog(index, query_text, limit=SEARCH_RESULT_LIMIT):
    """Tokenized fuzzy search over a prebuilt CatalogIndex.

    The inverted index narrows the candidates to products sharing at least
    one normalized token with the query, instead of scanning the entire
    catalog on every call. Ranks by how many normalized query words appear
    in the product name (word order and extra leftover terms don't prevent
    a match), then by shorter names, then by overall string similarity.
    """
    query_words = _search_words(query_text)
    if not query_words:
        return []

    candidate_ids = set()
    for word in query_words:
        candidate_ids.update(index.inverted.get(word, ()))

    scored = []
    for pid in candidate_ids:
        name_words = index.tokens_by_id[pid]
        match_count = sum(1 for w in query_words if w in name_words)
        ratio = difflib.SequenceMatcher(None, " ".join(query_words), " ".join(name_words)).ratio()
        scored.append((match_count, -len(name_words), ratio, index.by_id[pid]))

    scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    return [{"product_id": p["product_id"], "name": p["name"]} for _, _, _, p in scored[:limit]]


def _find_exact_matches(index, lines):
    """Splits each line's trailing quantity, then looks up the remaining
    name in the exact-match index (case/whitespace-insensitive).

    Returns (exact_items, remaining_lines): exact_items are finished item
    dicts for lines with exactly one matching catalog product — no search,
    no LLM. remaining_lines are raw lines that still need the normal
    fuzzy/LLM path: either no exact match, or an ambiguous one (the catalog
    has more than one product with that exact name) — never auto-accepted.
    """
    exact_items = []
    remaining_lines = []
    for raw_text in lines:
        split = _split_trailing_quantity(raw_text)
        matches = index.by_exact_name.get(_normalize_exact(split["name_query"]), [])
        if len(matches) == 1:
            p = matches[0]
            exact_items.append({
                "raw_text": raw_text,
                "matched_product_id": p["product_id"],
                "candidates": [],
                "name": p["name"],
                "quantity": split["quantity"],
                "fields": {},
                "code": p.get("code"),
                "exact_match": True,
            })
        else:
            remaining_lines.append(raw_text)
    return exact_items, remaining_lines


# ---------------------------------------------------------------------------
# File-backed catalog cache. The product catalog is only fetched live from
# the Sales Intellect API when there's no cache file yet, or when an operator
# explicitly hits POST /catalog/refresh — never automatically on a schedule.
# Products change once or twice every 2-3 weeks, so a stale local copy in
# between is an acceptable trade for not re-paginating ~2,900 products on
# every /parse and /confirm.
# ---------------------------------------------------------------------------

CATALOG_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog_cache.json")

_catalog_lock = threading.Lock()
_catalog_state = {"products": None, "fetched_at": None, "index": None, "checked_disk": False}

NO_CATALOG_CACHE_MESSAGE = 'No catalog cache found on server. Click "Refresh Catalog" to load it.'

# Tier-1 lookup cache: normalized product name -> list of previously-resolved
# matches (a list, not a single match, because a "group reference" line like
# "all pink leotards" legitimately resolves to several products sharing one
# raw_text). Cleared whenever the catalog is refreshed, since product ids
# could no longer be valid.
LOOKUP_CACHE_SIZE = 70
_lookup_cache = LFUCache(LOOKUP_CACHE_SIZE)


def _catalog_fields(p):
    return {
        "product_id": p.get("id"),
        "name": p.get("product_name"),
        "code": p.get("product_code"),
        "cost": p.get("cost") or 0,
    }


def _fetch_and_store_catalog():
    with timing.section("REFRESHING CATALOG FROM API"):
        raw_products = si_client.list_products()
        products = [_catalog_fields(p) for p in raw_products]
        fetched_at = datetime.now().isoformat(timespec="seconds")
        with timing.timed("write_catalog_cache_file", products=len(products)):
            with open(CATALOG_CACHE_PATH, "w") as f:
                json.dump({"fetched_at": fetched_at, "products": products}, f)
    return products, fetched_at


def _load_catalog_from_file():
    if not os.path.exists(CATALOG_CACHE_PATH):
        return None
    with timing.timed("read_catalog_cache_file"):
        with open(CATALOG_CACHE_PATH) as f:
            data = json.load(f)
    return data.get("products"), data.get("fetched_at")


def get_catalog():
    """Returns (products, fetched_at). Checks the cache file at most once per
    process (then serves from memory). Never fetches live on its own — if no
    cache file exists yet, returns (None, None) rather than hitting the API;
    only POST /catalog/refresh (refresh_catalog(), below) does a live fetch.
    """
    with _catalog_lock:
        if not _catalog_state["checked_disk"]:
            loaded = _load_catalog_from_file()
            if loaded is not None:
                _catalog_state["products"], _catalog_state["fetched_at"] = loaded
                _catalog_state["index"] = CatalogIndex(_catalog_state["products"])
                timing.log("catalog_loaded_from_file", products=len(_catalog_state["products"]))
            else:
                timing.log("catalog_cache_file_not_found")
            _catalog_state["checked_disk"] = True
        return _catalog_state["products"], _catalog_state["fetched_at"]


def get_catalog_index():
    """Returns the in-memory CatalogIndex, or None if no catalog cache has
    ever been loaded/fetched yet (see get_catalog())."""
    get_catalog()
    return _catalog_state["index"]


def refresh_catalog():
    """Forces a live re-fetch, overwrites the cache file, rebuilds the
    in-memory index, and clears the tier-1 lookup cache (its matched
    product ids are only guaranteed valid against the catalog snapshot
    they were resolved from). The only code path that calls the live API.
    """
    with _catalog_lock:
        _catalog_state["products"], _catalog_state["fetched_at"] = _fetch_and_store_catalog()
        _catalog_state["index"] = CatalogIndex(_catalog_state["products"])
        _catalog_state["checked_disk"] = True
        _lookup_cache.clear()
        timing.log("lookup_cache_cleared (catalog refreshed)")
        return _catalog_state["products"], _catalog_state["fetched_at"]


def _find_cached_matches(lines, action):
    """Tier 1: resolves lines whose normalized name was already resolved by
    a previous tier-2/tier-3 match, via the LFU lookup cache. Returns
    (cache_items, remaining_lines), same shape convention as
    _find_exact_matches / _find_confident_fuzzy_matches.
    """
    cache_items = []
    remaining_lines = []
    for raw_text in lines:
        split = _split_trailing_quantity(raw_text)
        key = _normalize_exact(split["name_query"])
        cached_matches = _lookup_cache.get(key)
        if not cached_matches:
            remaining_lines.append(raw_text)
            continue
        for match in cached_matches:
            cache_items.append({
                "raw_text": raw_text,
                "matched_product_id": match["matched_product_id"],
                "candidates": [],
                "name": match["name"],
                "quantity": split["quantity"],
                "fields": {},
                "code": match["code"],
                "exact_match": False,
                "cache_hit": True,
                "action": action,
            })
    return cache_items, remaining_lines


def _cache_match(name_query, matches):
    """Records a resolved match (or list of matches, for a group reference)
    in the tier-1 lookup cache, keyed by the same normalization used for
    exact-match lookups. Skips unmatched (matched_product_id is None) lines
    — caching a "not found" would silently keep masking a product added to
    the catalog later.
    """
    matched = [m for m in matches if m.get("matched_product_id")]
    if not matched:
        return
    key = _normalize_exact(name_query)
    entries = [
        {"matched_product_id": m["matched_product_id"], "name": m.get("name", ""), "code": m.get("code")}
        for m in matched
    ]
    existing = _lookup_cache.get(key) or []
    _lookup_cache.put(key, existing + entries)


def _find_confident_fuzzy_matches(index, lines, action):
    """Tier 2: a deterministic (non-LLM) auto-match. Auto-accepts a line only
    if exactly one catalog product achieves FULL query-token coverage (every
    normalized word in the line's name portion appears in that product's
    name) — a unique, confident winner. Zero or multiple such candidates
    (including genuine group references like "all pink leotards," which
    naturally match several products on the same tokens) fall through to the
    LLM agent unchanged. These never get exact_match=True — that's reserved
    for true 100% string matches (_find_exact_matches).
    """
    fuzzy_items = []
    remaining_lines = []
    for raw_text in lines:
        split = _split_trailing_quantity(raw_text)
        query_words = _search_words(split["name_query"])
        if not query_words:
            remaining_lines.append(raw_text)
            continue

        candidate_ids = set()
        for word in query_words:
            candidate_ids.update(index.inverted.get(word, ()))

        full_coverage = [
            pid for pid in candidate_ids
            if all(w in index.tokens_by_id[pid] for w in query_words)
        ]

        if len(full_coverage) != 1:
            remaining_lines.append(raw_text)
            continue

        p = index.by_id[full_coverage[0]]
        fuzzy_items.append({
            "raw_text": raw_text,
            "matched_product_id": p["product_id"],
            "candidates": [],
            "name": p["name"],
            "quantity": split["quantity"],
            "fields": {},
            "code": p.get("code"),
            "exact_match": False,
            "fuzzy_auto_match": True,
            "action": action,
        })
        _cache_match(split["name_query"], [fuzzy_items[-1]])
    return fuzzy_items, remaining_lines


def _run_parse_agent(mode, text, index):
    """Runs the Claude Agent SDK's built-in tool-call loop to turn `text` into items.

    For goods_received/product_updates, lines with an unambiguous exact
    catalog-name match are resolved directly by _find_exact_matches() and
    never reach the LLM; only the remaining lines are sent to the agent (if
    none remain, the agent isn't invoked at all). Tools are scoped per
    request: search_products (all modes), generate_next_product_code
    (new_products only), and submit_result (schema depends on mode). The
    agent calls submit_result once per line item; each call is captured
    here and translated into our canonical item schema.
    """
    with timing.section("PARSE AGENT", mode=mode, input_chars=len(text)):
        collected_items = []
        used_codes = {p.get("code") for p in index.catalog if p.get("code")}
        tool_call_counts = {}

        def _record_call(name):
            tool_call_counts[name] = tool_call_counts.get(name, 0) + 1
            return tool_call_counts[name]

        exact_items = []
        cache_items = []
        fuzzy_items = []
        if mode in ("goods_received", "product_updates"):
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            with timing.timed("find_exact_matches", lines=len(lines)):
                exact_items, remaining_lines = _find_exact_matches(index, lines)
            timing.log(
                "exact_match_result",
                total_lines=len(lines), exact=len(exact_items), remaining=len(remaining_lines),
            )
            action = "goods_received" if mode == "goods_received" else "stock_update"
            for item in exact_items:
                item["action"] = action

            if remaining_lines:
                with timing.timed("find_cached_matches", lines=len(remaining_lines)):
                    cache_items, remaining_lines = _find_cached_matches(remaining_lines, action)
                timing.log("cache_match_result", hits=len(cache_items), remaining=len(remaining_lines))

            if remaining_lines:
                with timing.timed("find_confident_fuzzy_matches", lines=len(remaining_lines)):
                    fuzzy_items, remaining_lines = _find_confident_fuzzy_matches(index, remaining_lines, action)
                timing.log("fuzzy_auto_match_result", auto_matched=len(fuzzy_items), remaining=len(remaining_lines))

            if not remaining_lines:
                timing.log("agent_skipped -- all lines resolved by exact/cache/fuzzy match")
                return exact_items + cache_items + fuzzy_items

            text = "\n".join(remaining_lines)

        # Accumulates search_products results since the last submit_result call,
        # so submit_result can reuse them as `candidates` instead of re-searching.
        pending_candidates = []

        def _merge_candidates(new_results):
            seen = {c["product_id"] for c in pending_candidates}
            for c in new_results:
                if c["product_id"] not in seen:
                    pending_candidates.append(c)
                    seen.add(c["product_id"])

        @tool(
            "search_products",
            "Fuzzy/substring search the product catalog by name. Returns up to "
            f"{SEARCH_RESULT_LIMIT} candidate matches as a JSON list of "
            '{"product_id", "name"}.',
            {"query": str},
        )
        async def search_products(args):
            call_num = _record_call("search_products")
            with timing.timed(f"tool:search_products #{call_num}", query=args.get("query", "")):
                results = _search_catalog(index, args.get("query", ""))
                _merge_candidates(results)
                text_out = json.dumps(results) if results else "No matching products found."
            timing.log("  -> results", count=len(results))
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
                call_num = _record_call("generate_next_product_code")
                with timing.timed(f"tool:generate_next_product_code #{call_num}"):
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
                call_num = _record_call("submit_result")
                with timing.timed(f"tool:submit_result #{call_num}", raw_text=args.get("raw_text", "")):
                    collected_items.append({
                        "action": "new_product",
                        "raw_text": args["raw_text"],
                        "matched_product_id": None,
                        "candidates": [],
                        "name": args.get("suggested_name", ""),
                        "quantity": args.get("quantity"),
                        "fields": args.get("fields") or {},
                        "code": args.get("suggested_product_code"),
                        "exact_match": False,
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
                call_num = _record_call("normalize_stock_line")
                with timing.timed(f"tool:normalize_stock_line #{call_num}", raw_text=args.get("raw_text", "")):
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
                call_num = _record_call("submit_result")
                with timing.timed(f"tool:submit_result #{call_num}", raw_text=args.get("raw_text", "")):
                    matched_id = args.get("matched_product_id")
                    matched = index.by_id.get(matched_id)
                    candidates = list(pending_candidates)
                    pending_candidates.clear()
                    collected_items.append({
                        "action": "goods_received",
                        "raw_text": args["raw_text"],
                        "matched_product_id": matched_id,
                        "candidates": candidates,
                        "name": matched["name"] if matched else "",
                        "quantity": args.get("quantity"),
                        "fields": {},
                        "code": matched["code"] if matched else None,
                        "exact_match": False,
                    })
                    _cache_match(
                        _split_trailing_quantity(args["raw_text"])["name_query"],
                        [collected_items[-1]],
                    )
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
                call_num = _record_call("normalize_stock_line")
                with timing.timed(f"tool:normalize_stock_line #{call_num}", raw_text=args.get("raw_text", "")):
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
                call_num = _record_call("submit_result")
                with timing.timed(f"tool:submit_result #{call_num}", raw_text=args.get("raw_text", "")):
                    matched_id = args.get("matched_product_id")
                    matched = index.by_id.get(matched_id)
                    candidates = list(pending_candidates)
                    pending_candidates.clear()
                    collected_items.append({
                        "action": "stock_update",
                        "raw_text": args["raw_text"],
                        "matched_product_id": matched_id,
                        "candidates": candidates,
                        "name": matched["name"] if matched else "",
                        "quantity": args.get("quantity"),
                        "fields": {},
                        "code": matched["code"] if matched else None,
                        "exact_match": False,
                    })
                    _cache_match(
                        _split_trailing_quantity(args["raw_text"])["name_query"],
                        [collected_items[-1]],
                    )
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
            msg_num = 0
            last_ts = time.perf_counter()
            async for message in query(prompt=text, options=options):
                now = time.perf_counter()
                msg_num += 1
                timing.log(f"agent_sdk_message #{msg_num} {type(message).__name__}", dur=now - last_ts)
                last_ts = now
                if isinstance(message, ResultMessage) and message.is_error:
                    raise Exception(message.result or "Agent run ended in an error state.")

        with timing.timed("agent_sdk_query_loop_total"):
            asyncio.run(_run())

        timing.log(
            "agent_tool_call_summary",
            total_tool_calls=sum(tool_call_counts.values()),
            by_tool=tool_call_counts,
            items_collected=len(collected_items),
        )
        return exact_items + cache_items + fuzzy_items + collected_items


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

    run_id = timing.new_run_id()
    with timing.section("PARSE REQUEST", run_id=run_id, mode=mode, text_chars=len(text)):
        # Served from the file-backed cache — search_products and
        # generate_next_product_code both search this same in-memory snapshot.
        # Only refetched live from the API via POST /catalog/refresh.
        try:
            catalog, fetched_at = get_catalog()
        except Exception as e:
            timing.log("catalog_fetch_failed", error=str(e))
            return jsonify({"error": f"Failed to load product catalog: {e}"}), 502
        if catalog is None:
            timing.log("catalog_not_found")
            return jsonify({"error": NO_CATALOG_CACHE_MESSAGE}), 409
        index = get_catalog_index()
        timing.log("catalog_index_ready", catalog_size=len(catalog), fetched_at=fetched_at)

        try:
            items = _run_parse_agent(mode, text, index)
        except Exception as e:
            timing.log("agent_run_failed", error=str(e))
            return jsonify({"error": f"Agent run failed: {e}"}), 502

        with timing.timed("assign_codes"):
            items = _assign_codes(items, catalog, reserve_existing_item_codes=True)

        timing.log("PARSE REQUEST RESULT", items_returned=len(items))

    return jsonify({"items": items})


@app.route("/catalog", methods=["GET"])
def get_catalog_route():
    """Serves the cached product catalog — never hits the live Sales
    Intellect API itself (see get_catalog()). Used by the frontend's review
    tables instead of calling the API directly, so the whole app shares one
    cached snapshot. If no cache file has ever been written, reports
    cache_found=False instead of fetching live — the frontend prompts the
    operator to click "Refresh Catalog" rather than silently blocking.
    """
    if si_client is None:
        return jsonify({"error": si_client_error}), 500
    try:
        products, fetched_at = get_catalog()
    except Exception as e:
        return jsonify({"error": f"Failed to load product catalog: {e}"}), 502
    if products is None:
        return jsonify({
            "products": [], "fetched_at": None, "count": 0,
            "cache_found": False, "message": NO_CATALOG_CACHE_MESSAGE,
        })
    return jsonify({
        "products": products, "fetched_at": fetched_at, "count": len(products),
        "cache_found": True,
    })


@app.route("/catalog/refresh", methods=["POST"])
def refresh_catalog_route():
    """Operator-triggered refresh — the only path that re-fetches the
    catalog live from the Sales Intellect API after initial startup.
    """
    if si_client is None:
        return jsonify({"error": si_client_error}), 500
    try:
        products, fetched_at = refresh_catalog()
    except Exception as e:
        return jsonify({"error": f"Failed to refresh product catalog: {e}"}), 502
    return jsonify({"products": products, "fetched_at": fetched_at, "count": len(products)})


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

    run_id = timing.new_run_id()
    with timing.section("CONFIRM REQUEST", run_id=run_id, items=len(items)):
        shop_name = None
        if shop_id:
            with timing.timed("lookup_shop_name"):
                try:
                    shops_resp = si_client.list_shops()
                    shop_list = shops_resp.get("shops", []) if isinstance(shops_resp, dict) else shops_resp
                    shop_name = next((s.get("shop_name") for s in shop_list if s.get("id") == shop_id), None)
                except Exception:
                    pass  # Falls back to "this shop" in error messages below; not worth failing the whole confirm over.

        results = []
        grn_items = [item for item in items if item.get("action") == "goods_received"]
        other_items = [item for item in items if item.get("action") != "goods_received"]
        timing.log("split_items", grn_items=len(grn_items), other_items=len(other_items))

        if other_items:
            with timing.section("SUBMITTING RESULTS", count=len(other_items)):
                for item in other_items:
                    action = item.get("action")
                    raw_text = item.get("raw_text", "")
                    name = item.get("name") or raw_text
                    with timing.timed(f"item:{action}", name=name):
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
            with timing.section("SUBMITTING GRN", grn_items=len(grn_items)):
                results.extend(_submit_grn(grn_items, shop_id, supplier_id, shop_name))

    return jsonify({"results": results})


def _verify_github_signature(secret, payload_body, signature_header):
    """Constant-time check of GitHub's HMAC-SHA256 webhook signature."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), payload_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@app.route("/deploy", methods=["POST"])
def deploy_webhook():
    """GitHub webhook target: on a push to main, schedules deploy/update.sh
    to run as an independent, detached systemd unit (see
    deploy/trigger_deploy.sh) — detached because this request is served by
    the very Flask service that update.sh restarts, so running it in-process
    would get killed mid-restart.

    This endpoint sits outside Cloudflare Access (GitHub can't complete an
    interactive login), so DEPLOY_WEBHOOK_SECRET + the signature check below
    is its only protection. It never runs anything other than the fixed
    update.sh script — a valid signature can trigger a redeploy of whatever
    is already on the main branch, not arbitrary commands.
    """
    if not DEPLOY_WEBHOOK_SECRET:
        return jsonify({"error": "DEPLOY_WEBHOOK_SECRET is not configured."}), 503

    signature = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_github_signature(DEPLOY_WEBHOOK_SECRET, request.get_data(), signature):
        return jsonify({"error": "Invalid signature."}), 401

    if request.headers.get("X-GitHub-Event") != "push":
        return jsonify({"status": "ignored: not a push event"}), 200

    payload = request.get_json(silent=True) or {}
    if payload.get("ref") != "refs/heads/main":
        return jsonify({"status": f"ignored: ref {payload.get('ref')!r} is not main"}), 200

    try:
        subprocess.Popen(["/usr/bin/sudo", DEPLOY_TRIGGER_SCRIPT])
    except Exception as e:
        app.logger.exception("Failed to launch deploy trigger script")
        return jsonify({"error": f"Could not start deploy: {e}"}), 500

    return jsonify({"status": "deploy scheduled"}), 202


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
    timing.log("grn_item_split", incomplete=len(incomplete), complete=len(complete))

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

    # Cost comes from the cached catalog (see get_catalog()) — no separate
    # API call, and the cache stores cost per product already.
    cost_by_id = {}
    with timing.timed("fetch_product_costs"):
        try:
            catalog, _ = get_catalog()
            if catalog is not None:
                cost_by_id = {p["product_id"]: p.get("cost") or 0 for p in catalog}
            else:
                app.logger.warning("No catalog cache available for GRN cost lookup; defaulting unit_cost to 0.")
        except Exception:
            app.logger.exception("Could not load product costs for GRN; defaulting unit_cost to 0.")

    with timing.timed("build_grn_payload", line_items=len(complete)):
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
        with timing.timed("create_grn_api_call", line_items=len(grn_line_items)):
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
