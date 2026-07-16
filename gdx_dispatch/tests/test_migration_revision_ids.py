"""Alembic revision-ID invariants.

``alembic_version.version_num`` is varchar(32); a longer revision ID passes
every test that doesn't actually stamp it, then fails the real upgrade with
StringDataRightTruncation at deploy time (main went unreleasable 2026-07-16
when ``026_outlook_vendor_bill_allowlist`` — 33 chars — landed). Parse every
migration file's identifiers statically so the limit is enforced at PR time.
"""
from __future__ import annotations

import re
from pathlib import Path

VERSIONS_DIR = Path(__file__).resolve().parents[1] / "migrations" / "versions"
ALEMBIC_VERSION_NUM_MAX = 32  # alembic's default varchar(32) version table

_REVISION_RE = re.compile(r'^revision\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
_DOWN_RE = re.compile(r'^down_revision\s*=\s*(?:["\']([^"\']+)["\']|None)', re.MULTILINE)


def _migrations() -> dict[str, str | None]:
    """{revision: down_revision} parsed from every versions/*.py file."""
    chain: dict[str, str | None] = {}
    files = sorted(VERSIONS_DIR.glob("*.py"))
    assert files, f"no migration files found under {VERSIONS_DIR}"
    for path in files:
        text = path.read_text(encoding="utf-8")
        rev = _REVISION_RE.search(text)
        assert rev, f"{path.name}: no revision assignment found"
        down = _DOWN_RE.search(text)
        assert down, f"{path.name}: no down_revision assignment found"
        # House convention: filename == revision ID. A rename that misses the
        # revision string (or vice versa) is exactly how a "fixed" ID ships
        # the old value.
        assert path.stem == rev.group(1), (
            f"{path.name}: filename and revision ID diverge ({rev.group(1)!r})"
        )
        chain[rev.group(1)] = down.group(1)  # None-group when literal None
    return chain


def test_revision_ids_fit_alembic_version_column():
    too_long = {
        rev: len(rev)
        for rev in _migrations()
        if len(rev) > ALEMBIC_VERSION_NUM_MAX
    }
    assert too_long == {}, (
        f"revision IDs exceed varchar({ALEMBIC_VERSION_NUM_MAX}) and will fail "
        f"the version stamp at upgrade time: {too_long}"
    )


def test_down_revisions_resolve_to_a_single_chain():
    chain = _migrations()
    for rev, down in chain.items():
        if down is not None:
            assert down in chain, (
                f"{rev}: down_revision {down!r} matches no migration file "
                "(dangling after a rename?)"
            )
    roots = [rev for rev, down in chain.items() if down is None]
    assert len(roots) == 1, f"expected exactly one base migration, got {roots}"
    heads = set(chain) - {down for down in chain.values() if down is not None}
    assert len(heads) == 1, f"expected exactly one head migration, got {sorted(heads)}"
