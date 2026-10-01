"""Simple client for the Sales Intellect POS API."""

import os
import time

import requests

import timing

BASE_URL = "https://api.salesintellectpos.com/v1.0"

# Shared page size for the API's cursor-based listing endpoints (/products,
# /inventory) — both require limit/cursor in the JSON body, not query params.
PAGE_LIMIT = 250


class SalesIntellectClient:
    def __init__(self, token=None):
        self.token = token or os.environ.get("SI_API_TOKEN")
        if not self.token:
            raise ValueError(
                "No API token provided. Pass one to SalesIntellectClient() "
                "or set the SI_API_TOKEN environment variable."
            )
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {self.token}"})

    def _request(self, method, path, **kwargs):
        t0 = time.perf_counter()
        response = self.session.request(method, f"{BASE_URL}{path}", **kwargs)
        dur = time.perf_counter() - t0
        if not response.ok:
            timing.log(f"API {method} {path} FAILED", dur=dur, status=response.status_code)
            raise Exception(
                f"{method} {path} failed with status {response.status_code}: {response.text}"
            )
        timing.log(f"API {method} {path}", dur=dur, status=response.status_code)
        return response.json()

    def list_products(self):
        """Fetch every product, paging through the API's cursor-based listing.

        `limit`/`cursor` must be sent as a JSON body on this GET request —
        despite the API docs listing them as query parameters, the API
        silently ignores query params for them and falls back to a default
        page of 10. Its `cursor` is also unreliable as a stop signal: it
        keeps minting a new non-null cursor even past the last page (we
        verified this empirically), so the only safe loop-termination
        condition is an empty `products` list, never "cursor is missing" or
        "cursor didn't change".
        """
        with timing.section("FETCHING PRODUCT CATALOG (list_products)"):
            products = []
            cursor = None
            page_num = 0
            while True:
                page_num += 1
                body = {"limit": PAGE_LIMIT}
                if cursor is not None:
                    body["cursor"] = cursor
                page = self._request("GET", "/products", json=body)
                page_products = page.get("products", [])
                timing.log(f"page {page_num}", returned=len(page_products))
                if not page_products:
                    break
                products.extend(page_products)
                cursor = page.get("cursor")
            timing.log("list_products complete", pages=page_num, total_products=len(products))
        return products

    def upsert_product(self, data):
        return self._request("POST", "/products", json=data)

    def list_shops(self):
        return self._request("GET", "/shops")

    def list_suppliers(self):
        """Fetch every supplier, paging like list_products()."""
        with timing.section("FETCHING SUPPLIERS (list_suppliers)"):
            suppliers = []
            cursor = None
            page_num = 0
            while True:
                page_num += 1
                body = {"limit": PAGE_LIMIT}
                if cursor is not None:
                    body["cursor"] = cursor
                page = self._request("GET", "/suppliers", json=body)
                page_suppliers = page.get("suppliers", [])
                timing.log(f"page {page_num}", returned=len(page_suppliers))
                if not page_suppliers:
                    break
                suppliers.extend(page_suppliers)
                cursor = page.get("cursor")
            timing.log("list_suppliers complete", pages=page_num, total_suppliers=len(suppliers))
        return suppliers

    def create_grn(self, data):
        return self._request("POST", "/grn", json=data)

    def get_inventory(self, shop_id):
        """Fetch every inventory level for a shop, paging like list_products().

        Same API quirk as /products: shop_ids/limit/cursor must be sent as a
        JSON body on this GET request, not query params, or the API caps
        results at a default page of 10 (confirmed empirically — query
        params silently returned only the 10 lowest product codes,
        regardless of the requested shop). cursor keeps minting a new
        non-null value past the last page too, so termination is on an
        empty inventory_levels list only.
        """
        with timing.section("FETCHING INVENTORY (get_inventory)", shop_id=shop_id):
            levels = []
            cursor = None
            page_num = 0
            while True:
                page_num += 1
                body = {"shop_ids": [shop_id], "limit": PAGE_LIMIT}
                if cursor is not None:
                    body["cursor"] = cursor
                page = self._request("GET", "/inventory", json=body)
                page_levels = page.get("inventory_levels", [])
                timing.log(f"page {page_num}", returned=len(page_levels))
                if not page_levels:
                    break
                levels.extend(page_levels)
                cursor = page.get("cursor")
            timing.log("get_inventory complete", pages=page_num, total_levels=len(levels))
        return levels

    def set_inventory(self, shop_id, product_id, in_stock):
        body = {
            "inventory_levels": [
                {"shop_id": shop_id, "product_id": product_id, "in_stock": in_stock}
            ]
        }
        return self._request("POST", "/inventory", json=body)

    def adjust_inventory(self, shop_id, product_id, delta):
        with timing.section("ADJUST INVENTORY", shop_id=shop_id, product_id=product_id, delta=delta):
            levels = self.get_inventory(shop_id)
            if isinstance(levels, dict):
                levels = levels.get("inventory_levels", [])
            current = None
            for level in levels:
                if level.get("product_id") == product_id:
                    current = level.get("in_stock")
                    break
            if current is None:
                raise Exception(
                    f"Could not find current inventory for product_id={product_id} "
                    f"at shop_id={shop_id}"
                )
            new_total = current + delta
            return self.set_inventory(shop_id, product_id, new_total)
