"""Unit tests for gdx_dispatch.core.name_normalize.humanize_name."""
from __future__ import annotations

import pytest

from gdx_dispatch.core.name_normalize import humanize_name


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Lowercase fix-ups
        ("mike wendt", "Mike Wendt"),
        ("aaron conn", "Aaron Conn"),
        ("tom heinze", "Tom Heinze"),
        ("Tim loose", "Tim Loose"),
        ("Henning lumber yard", "Henning Lumber Yard"),
        # Already-correct title case
        ("Mike Wendt", "Mike Wendt"),
        ("McDonald's", "McDonald's"),
        # Acronyms (all-uppercase, no change)
        ("PPFD", "PPFD"),
        ("AT&T", "AT&T"),
        # Mixed-case left alone
        ("iPhone", "iPhone"),
        # Embedded digits don't break the all-lowercase check
        ("89 Lumber and supply", "89 Lumber And Supply"),
        # Leading/trailing whitespace stripped
        ("  jane doe  ", "Jane Doe"),
        # Edge cases
        ("", ""),
        (" ", ""),
    ],
)
def test_humanize_name_round_trip(raw: str, expected: str) -> None:
    assert humanize_name(raw) == expected


def test_humanize_name_none() -> None:
    assert humanize_name(None) is None


def test_humanize_name_single_word() -> None:
    assert humanize_name("alex") == "Alex"
    assert humanize_name("Alex") == "Alex"
    assert humanize_name("DOUG") == "DOUG"
