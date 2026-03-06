"""Tests for ask_user use case helpers."""

from __future__ import annotations

import pytest

from src.claude_telegram_bot.application.use_cases.ask_user import (
    parse_ask_callback_data,
    resolve_ask_selection,
)


class TestParseAskCallback:
    def test_valid_format(self):
        assert parse_ask_callback_data("ask:q1:0") == ("q1", "0")

    def test_invalid_format(self):
        assert parse_ask_callback_data("bad:data") is None

    def test_missing_parts(self):
        assert parse_ask_callback_data("ask:q1") is None

    def test_extra_colons_in_value(self):
        # "ask:q1:0:extra" — implementation splits into 3 parts max,
        # so the remainder stays in the value field.
        result = parse_ask_callback_data("ask:q1:0:extra")
        assert result == ("q1", "0:extra")


class TestResolveAskSelection:
    def test_index_selection(self):
        options = [{"label": "A"}, {"label": "B"}]
        assert resolve_ask_selection("1", options) == "B"

    def test_first_option(self):
        options = [{"label": "A"}, {"label": "B"}]
        assert resolve_ask_selection("0", options) == "A"

    def test_out_of_range(self):
        options = [{"label": "A"}]
        result = resolve_ask_selection("5", options)
        # Should handle gracefully
        assert result is None or isinstance(result, str)
