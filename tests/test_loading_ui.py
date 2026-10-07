"""Unit tests for loading_ui.py's busy-flag session_state helpers and the
overlay() context manager's "never left stuck on screen" guarantee.
"""

import sys
from pathlib import Path
from unittest import mock

import pytest
import streamlit as st

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import loading_ui  # noqa: E402


@pytest.fixture(autouse=True)
def clean_session_state():
    st.session_state.clear()
    yield
    st.session_state.clear()


def test_is_busy_defaults_to_false():
    assert loading_ui.is_busy("grn_parse") is False


def test_start_busy_sets_the_flag():
    loading_ui.start_busy("grn_parse")
    assert loading_ui.is_busy("grn_parse") is True


def test_start_busy_stashes_payload_readable_via_busy_payload():
    loading_ui.start_busy("grn_parse", paste_text="hello", mode="goods_received")
    assert loading_ui.busy_payload("grn_parse", "paste_text") == "hello"
    assert loading_ui.busy_payload("grn_parse", "mode") == "goods_received"


def test_busy_payload_for_unset_key_is_none():
    assert loading_ui.busy_payload("grn_parse", "nope") is None


def test_clear_busy_resets_flag_and_removes_payload():
    loading_ui.start_busy("grn_parse", paste_text="hello")
    loading_ui.clear_busy("grn_parse", "paste_text")

    assert loading_ui.is_busy("grn_parse") is False
    assert loading_ui.busy_payload("grn_parse", "paste_text") is None


def test_different_keys_do_not_collide():
    loading_ui.start_busy("grn_parse")
    assert loading_ui.is_busy("grn_confirm") is False


def test_overlay_clears_placeholder_on_normal_exit():
    fake_placeholder = mock.Mock()
    with mock.patch.object(st, "empty", return_value=fake_placeholder):
        with loading_ui.overlay("Doing a thing…"):
            pass

    fake_placeholder.markdown.assert_called_once()
    fake_placeholder.empty.assert_called_once()


def test_overlay_clears_placeholder_even_when_body_raises():
    """The core 'never left stuck on screen' guarantee: an exception
    inside the `with overlay(...):` block must not leave the scrim up."""
    fake_placeholder = mock.Mock()
    with mock.patch.object(st, "empty", return_value=fake_placeholder):
        with pytest.raises(ValueError):
            with loading_ui.overlay("Doing a thing…"):
                raise ValueError("boom")

    fake_placeholder.empty.assert_called_once()


def test_overlay_message_is_rendered_into_the_markdown():
    fake_placeholder = mock.Mock()
    with mock.patch.object(st, "empty", return_value=fake_placeholder):
        with loading_ui.overlay("Submitting 12 item(s)…"):
            pass

    rendered_html = fake_placeholder.markdown.call_args[0][0]
    assert "Submitting 12 item(s)…" in rendered_html
