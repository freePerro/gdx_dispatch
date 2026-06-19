"""Slice 2 — vendor statement line classifier.

Asserts every line description from the real Midwest sample (`cs_master (41).PDF`,
27 lines) lands in the bucket Doug specified in the session-99 close note.
"""
from __future__ import annotations

import pytest

from gdx_dispatch.modules.vendor_statements.classifier import (
    INVENTORY,
    JOB,
    UNKNOWN,
    classify_line,
)


@pytest.mark.parametrize(
    "description",
    [
        "Stock",
        "Op's 1.16.26",
        "406x6x32",
        "2742 10x10's",
        "2.12.26",
        "2/26/26",
    ],
)
def test_inventory_descriptions(description: str) -> None:
    assert classify_line(description) == INVENTORY


@pytest.mark.parametrize(
    "description",
    [
        "LYNN & JIM LIEPOLD",
        "TREVOR JOHNSON",
        "Trevor Johnson",
        "RUSS BISCHOFF",
        "Katerie Broz",
        "Schwatz Jenson",
        "HBC Thompson",
        "YBUILT PETERSONS",
    ],
)
def test_job_descriptions(description: str) -> None:
    assert classify_line(description) == JOB


@pytest.mark.parametrize(
    "description",
    [
        "Add-ons",
        "Add-on",
        "add on",
        "Wilke",
        "Wilke 2",
    ],
)
def test_unknown_descriptions(description: str) -> None:
    assert classify_line(description) == UNKNOWN


@pytest.mark.parametrize("description", [None, "", "   "])
def test_empty_is_unknown(description) -> None:
    assert classify_line(description) == UNKNOWN
