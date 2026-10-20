"""Bulk Product Upload page: paste brand-new products to create. Kept out
of Home's navigation for now (its button is disabled there) — the page
itself stays fully wired so it's a one-line flip (enable the Home button)
once this is ready to turn on."""

import core

core.render_back_to_home()

client = core.require_client()
catalog_products = core.render_catalog_bar()

core.render_parse_tab(
    client, catalog_products=catalog_products,
    key_prefix="bulk",
    mode="new_products",
    subheader="Bulk Product Upload",
    caption=(
        "Paste a raw WhatsApp message listing brand new products to add "
        "(e.g. \"add new product: Kickboards Large, price 8.50\"). No shop "
        "needed — these are created as new products, not stock updates. Edit "
        "or delete a row in the table below before confirming; nothing is "
        "written to Sales Intellect until then."
    ),
    show_shop_selector=False,
)
