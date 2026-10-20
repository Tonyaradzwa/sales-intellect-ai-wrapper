"""Adjust Stock Counts page: paste stock changes for existing products,
then Increase/Decrease or Set stock. See core.render_parse_tab() and
core._render_inventory_rows()."""

import core

core.render_back_to_home()

client = core.require_client()
catalog_products = core.render_catalog_bar()

core.render_parse_tab(
    client, catalog_products=catalog_products,
    key_prefix="updates",
    mode="product_updates",
    subheader="Adjust Stock Counts",
    show_shop_selector=True,
)
