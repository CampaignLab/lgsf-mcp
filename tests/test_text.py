"""Tests for lgsf_mcp.text.window."""

import pytest

from lgsf_mcp.text import TextWindow, window


def test_empty_and_none() -> None:
    for text in (None, ""):
        win = window(text)
        assert win == TextWindow(
            text="", offset=0, text_length=0, has_more=False, next_offset=None
        )


def test_shorter_than_window() -> None:
    win = window("hello", offset=0, max_chars=10)
    assert win.text == "hello"
    assert win.text_length == 5
    assert not win.has_more
    assert win.next_offset is None


def test_exact_boundary() -> None:
    win = window("abcde", offset=0, max_chars=5)
    assert win.text == "abcde"
    assert not win.has_more
    assert win.next_offset is None


def test_middle_window() -> None:
    win = window("abcdefghij", offset=3, max_chars=4)
    assert win.text == "defg"
    assert win.offset == 3
    assert win.text_length == 10
    assert win.has_more
    assert win.next_offset == 7


def test_offset_past_end() -> None:
    win = window("abc", offset=3)
    assert win.text == ""
    assert win.text_length == 3
    assert not win.has_more
    assert win.next_offset is None
    win = window("abc", offset=100)
    assert win.text == ""
    assert not win.has_more


def test_invalid_arguments() -> None:
    with pytest.raises(ValueError):
        window("abc", offset=-1)
    with pytest.raises(ValueError):
        window("abc", max_chars=0)
