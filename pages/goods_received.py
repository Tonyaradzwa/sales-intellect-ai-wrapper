"""Goods Received page: paste a delivery note -> match against the
catalog -> submit a GRN. See core.render_parse_tab()."""

import core

core.render_back_to_home()

client = core.require_client()
catalog_products = core.render_catalog_bar()

core.render_parse_tab(
    client, catalog_products=catalog_products,
    key_prefix="grn",
    mode="goods_received",
    subheader="Submit GRN",
    # Implementation notes: all items in one submission are recorded as a
    # single GRN, and payment method is fixed to Cash — the only one
    # currently supported, see _GRN_CASH_PAYMENT_METHOD_ID in server.py.
    show_shop_selector=True,
    show_supplier_selector=True,
)
