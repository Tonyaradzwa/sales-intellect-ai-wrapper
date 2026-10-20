"""Landing page: buttons to each workflow. Entry point for navigation —
app.py hides the built-in sidebar page list in favor of this."""

import streamlit as st

GRN_KEY = "home_grn_btn"
INV_KEY = "home_inventory_btn"
BULK_KEY = "home_bulk_btn"

st.title("Sales Intellect AI Wrapper", anchor=False)
st.caption("Choose what you'd like to do.")

st.write("")

# Stacked, oversized buttons, default Streamlit coloring — just sized up
# so the label reads as a big tappable block instead of small text lost in
# a large button.
st.markdown(
    f"""
    <style>
    .st-key-{GRN_KEY}, .st-key-{INV_KEY}, .st-key-{BULK_KEY} {{
        margin-bottom: 0.85rem;
    }}
    .st-key-{GRN_KEY} button, .st-key-{INV_KEY} button, .st-key-{BULK_KEY} button {{
        height: 4.5rem;
        font-size: 2rem;
        font-weight: 700;
        border-radius: 0.75rem;
    }}
    </style>
    """,
    unsafe_allow_html=True,
)

# Narrower centered column so the stacked buttons don't stretch edge to
# edge on a wide screen.
_, center, _ = st.columns([1, 2, 1])
with center:
    if st.button("Submit GRN", key=GRN_KEY, use_container_width=True):
        st.switch_page("pages/goods_received.py")

    if st.button("Inventory Updates", key=INV_KEY, use_container_width=True):
        st.switch_page("pages/inventory_updates.py")

    st.button(
        "Bulk Product Upload", key=BULK_KEY, use_container_width=True,
        disabled=True, help="Coming soon",
    )
