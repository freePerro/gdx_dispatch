"""Codemod — migrate pre-PrimeVue-v4 CSS variable references to v4 tokens.

Doug 2026-05-10: the Re-open/Warranty popup was unreadable in dark mode
because the component used `var(--surface-0, #fff)` which fell back to
literal white, ignoring dark mode entirely. Fixed in commit 45bd3885.
A codebase grep surfaces 47 other .vue/.css files with the same pattern.

This codemod migrates each match. The mapping favors **semantic** tokens
(--p-content-background, --p-content-hover-background, --p-content-border-color,
--p-highlight-background, --p-text-muted-color) over numbered ones, because
the numbered v4 tokens (--p-surface-100, --p-surface-300) are STILL light-only
in this theme config — replacing one numbered token with another produces
the same dark-mode bug we're fixing.

Usage:
    python gdx_dispatch/frontend/scripts/migrate_legacy_tokens.py [--dry-run]

Idempotent: running twice produces zero additional changes.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "gdx_dispatch" / "frontend" / "src"

# Each entry: (regex pattern, replacement). Order matters — more-specific
# patterns must run before more-general ones (e.g. --primary-50 before
# bare --primary-color).
SUBS: list[tuple[str, str]] = [
    # ─── Theme-aware semantic tokens ─────────────────────────────────
    # Card/dialog surface (white in light, dark zinc in dark)
    (r"var\(--surface-0(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-content-background)"),
    # Hover background (light gray in light, dark zinc in dark)
    # --surface-50 / --surface-100 are typically used for hover or subtle
    # alternate row backgrounds. --p-content-hover-background is the
    # theme-aware role token.
    (r"var\(--surface-50(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-content-hover-background)"),
    (r"var\(--surface-100(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-content-hover-background)"),
    # Subtle border
    (r"var\(--surface-200(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-content-border-color)"),
    (r"var\(--surface-300(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-content-border-color)"),
    # Stronger border / divider
    (r"var\(--surface-400(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-content-border-color)"),
    # Highlight (selected card / active state)
    (r"var\(--primary-50(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-highlight-background)"),
    (r"var\(--primary-100(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-highlight-background)"),
    # Primary color (theme-aware in v4) — accept optional hex fallback
    (r"var\(--primary-color(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-primary-color)"),
    (r"var\(--primary-500(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-primary-500)"),
    (r"var\(--primary-600(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-primary-600)"),
    (r"var\(--primary-700(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-primary-700)"),
    # Muted text
    (r"var\(--text-color-secondary(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-text-muted-color)"),

    # ─── Color-named tokens — direct v4 equivalents ──────────────────
    # Red
    (r"var\(--red-50(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-red-50)"),
    (r"var\(--red-100(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-red-100)"),
    (r"var\(--red-200(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-red-200)"),
    (r"var\(--red-300(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-red-300)"),
    (r"var\(--red-400(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-red-400)"),
    (r"var\(--red-500(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-red-500)"),
    (r"var\(--red-600(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-red-600)"),
    (r"var\(--red-700(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-red-700)"),
    # --p-red-800/900 don't exist in v4; map to --p-red-700 (closest equivalent)
    (r"var\(--red-800(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-red-700)"),
    (r"var\(--red-900(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-red-700)"),

    # Green
    (r"var\(--green-50(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-green-50)"),
    (r"var\(--green-100(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-green-100)"),
    (r"var\(--green-200(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-green-200)"),
    (r"var\(--green-300(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-green-300)"),
    (r"var\(--green-400(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-green-400)"),
    (r"var\(--green-500(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-green-500)"),
    (r"var\(--green-600(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-green-600)"),
    (r"var\(--green-700(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-green-700)"),
    # --p-green-800/900 don't exist in v4; map to --p-green-700.
    (r"var\(--green-800(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-green-700)"),
    (r"var\(--green-900(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-green-700)"),

    # Blue
    (r"var\(--blue-50(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-blue-50)"),
    (r"var\(--blue-100(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-blue-100)"),
    (r"var\(--blue-200(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-blue-200)"),
    (r"var\(--blue-300(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-blue-300)"),
    (r"var\(--blue-400(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-blue-400)"),
    (r"var\(--blue-500(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-blue-500)"),
    (r"var\(--blue-600(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-blue-600)"),
    (r"var\(--blue-700(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-blue-700)"),
    # --p-blue-800/900 don't exist in v4; map to --p-blue-700.
    (r"var\(--blue-800(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-blue-700)"),
    (r"var\(--blue-900(?:\s*,\s*#[0-9a-fA-F]{3,6})?\)", "var(--p-blue-700)"),
]


def migrate_text(text: str) -> tuple[str, int]:
    total = 0
    for pat, repl in SUBS:
        text, n = re.subn(pat, repl, text)
        total += n
    return text, total


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    files = list(SRC.rglob("*.vue")) + list(SRC.rglob("*.css"))
    changed = 0
    total_subs = 0
    for path in sorted(files):
        try:
            original = path.read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            print(f"  ! skip {path.relative_to(REPO_ROOT)}: {e}")
            continue
        migrated, n = migrate_text(original)
        if n > 0:
            total_subs += n
            changed += 1
            rel = path.relative_to(REPO_ROOT)
            print(f"  {rel}: {n} substitution{'s' if n != 1 else ''}")
            if not dry_run:
                path.write_text(migrated, encoding="utf-8")
    verb = "would migrate" if dry_run else "migrated"
    print(f"\n{verb} {total_subs} reference{'s' if total_subs != 1 else ''} across {changed} file{'s' if changed != 1 else ''}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
