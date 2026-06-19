"""Phase 2 / C5 contract pin — sku-suggest returns part_id on inventory rows.

Doug 2026-05-10: the MobileJobCloseoutDialog reads `s.part_id` for
`source: 'parts'` suggestions and includes it in the closeout payload.
The C2 backend uses that part_id to write a JobPart row (inventory
ledger update). Pre-fix, sku-suggest didn't include part_id, so all
closeout parts routed as free-text snapshot-only — inventory math
never updated from closeouts.

This pin asserts the contract at the source level — if a future
refactor drops the field, the dialog silently degrades to snapshot-
only mode again.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _sku_suggest_function_body() -> str:
    """Return the source text of the sku_suggest function up to the
    next top-level def/decorator. Used by the assertions below."""
    src = (REPO_ROOT / "gdx_dispatch" / "routers" / "parts_needed.py").read_text(encoding="utf-8")
    start = src.find("def sku_suggest(")
    assert start > 0, "sku_suggest function not found"
    after = src[start:]
    # Walk to the next top-level definition / decorator.
    next_def = re.search(r"\n(?:def |@router\.)", after[1:])
    end = (next_def.start() + 1) if next_def else len(after)
    return after[:end]


def test_sku_suggest_returns_part_id_for_inventory_rows() -> None:
    span = _sku_suggest_function_body()
    # The "source": "parts" branch must include `part_id`.
    # Find the .append(... source: "parts" ...) block and confirm part_id.
    parts_branch = re.search(
        r'suggestions\.append\(\s*\{\s*"source"\s*:\s*"parts"[\s\S]*?\}\s*\)',
        span,
    )
    assert parts_branch, "couldn't locate source='parts' append block"
    block = parts_branch.group(0)
    assert '"part_id"' in block, (
        "sku-suggest's source='parts' response is missing part_id. The "
        "MobileJobCloseoutDialog reads this to decide whether the closeout "
        "writes a JobPart row (inventory math) or just a snapshot. "
        "Without part_id, all closeouts are snapshot-only."
    )
    assert "p.id" in block, (
        "part_id should come from p.id (the Part ORM instance). Recover by "
        "inspecting the for-loop variable name in the sku_suggest body."
    )


def test_sku_suggest_does_not_leak_part_id_for_door_catalog() -> None:
    """door_catalog and custom_door suggestions don't have a parts.id —
    they're separate tables. If sku-suggest erroneously emits part_id for
    them, the closeout endpoint would attempt a JobPart insert against a
    non-existent parts row → FK violation (the C2 hotfix bug)."""
    span = _sku_suggest_function_body()
    door_branch = re.search(
        r'suggestions\.append\(\s*\{\s*"source"\s*:\s*"door_catalog"[\s\S]*?\}\s*\)',
        span,
    )
    if door_branch:
        # door_catalog branch exists — must NOT include part_id.
        block = door_branch.group(0)
        assert '"part_id"' not in block, (
            "door_catalog suggestions are emitting part_id. They aren't "
            "in the parts table — closeout would FK-violate."
        )
