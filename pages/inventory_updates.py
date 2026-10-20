"""Inventory Updates page: paste stock changes for existing products, then
Bulk Increase/Decrease or set a Fixed Quantity. See core.render_parse_tab()
and core._render_inventory_rows()."""

import core

core.render_back_to_home()

client = core.require_client()
catalog_products = core.render_catalog_bar()

core.render_parse_tab(
    client, catalog_products=catalog_products,
    key_prefix="updates",
    mode="product_updates",
    subheader="Inventory Updates",
    caption=(
        "Paste a raw WhatsApp message describing stock changes for existing "
        "products, then choose how to apply it: Bulk Increase/Decrease Stock "
        "adds or subtracts the parsed quantity from current stock; Fixed "
        "Quantity sets stock to an exact number instead. Use Bulk Product "
        "Upload for brand new products. Edit or delete a row in the table "
        "below before confirming; nothing is written to Sales Intellect "
        "until then."
    ),
    show_shop_selector=True,
)
