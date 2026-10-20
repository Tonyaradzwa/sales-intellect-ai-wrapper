"""Streamlit multipage entrypoint. Run with ./run.sh or:
    export SI_API_TOKEN=your_token_here
    export ANTHROPIC_API_KEY=your_anthropic_key
    streamlit run app.py

Each workflow lives in its own file under pages/ — this file only sets up
page-wide chrome (page config, production banner, the client gate) and
wires up navigation. See core.py for the shared rendering logic every page
imports.
"""

import streamlit as st

import core
import loading_ui

st.set_page_config(page_title="Sales Intellect POS", layout="wide")
loading_ui.inject_global_css()

# Belt-and-suspenders against position="hidden" below: Streamlit's legacy
# auto-generated page list (built from the pages/ directory, independent of
# st.navigation) can briefly flash on a session's very first render, before
# the "hidden" setting takes effect. Force it hidden from the first paint.
st.markdown(
    '<style>[data-testid="stSidebarNav"] { display: none; }</style>',
    unsafe_allow_html=True,
)

if core.is_production():
    core.render_production_banner()

# Gated here too (not just per-page) so a broken SI_API_TOKEN fails fast
# before the user even sees the Home page's buttons.
core.require_client()

home_page = st.Page("pages/home.py", title="Home", default=True)
grn_page = st.Page("pages/goods_received.py", title="Submit GRN")
inventory_page = st.Page("pages/inventory_updates.py", title="Adjust Stock Counts")
bulk_upload_page = st.Page("pages/bulk_upload.py", title="Add New Products")

# position="hidden": no built-in sidebar page list — navigation is driven
# entirely by the Home page's buttons (and each page's "← Home" link), per
# the home-page-as-launcher design.
pg = st.navigation(
    [home_page, grn_page, inventory_page, bulk_upload_page],
    position="hidden",
)
pg.run()
