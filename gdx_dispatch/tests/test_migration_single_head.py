"""The migration history has exactly one head, and no two files share a number.

Parallel branches each add "the next" migration off the same parent (three
branches carried a ``121_*`` off ``120_completed_jobs_dispatch_done`` on
2026-10-13). Each is green alone; once two are on main, ``alembic upgrade
head`` refuses with "Multiple head revisions" — and the app entrypoint runs
that under ``set -e`` before serving, so prod crash-loops. Different file
names mean git never reports a conflict. This test is the only thing that
turns the second PR red.

Prior art: pytest-alembic's built-in ``test_single_head_revision`` asserts the
same thing; the repo already reads heads in ``pave_tenant_db.alembic_heads``,
so this reuses that rather than adding the plugin and its fixtures.
"""
from __future__ import annotations

import re
import shutil
from collections import defaultdict
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory

from gdx_dispatch.tools import pave_tenant_db

MIGRATIONS = pave_tenant_db.PACKAGE_DIR / "migrations"
_NUMBER = re.compile(r"^(\d+)_")


def _heads(migrations_dir: Path) -> set[str]:
    cfg = AlembicConfig(str(pave_tenant_db.PACKAGE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(migrations_dir))
    return set(ScriptDirectory.from_config(cfg).get_heads())


def _shared_numbers(versions_dir: Path) -> dict[str, list[str]]:
    by_number: dict[str, list[str]] = defaultdict(list)
    for path in versions_dir.glob("*.py"):
        m = _NUMBER.match(path.name)
        if m:
            by_number[m.group(1)].append(path.name)
    return {n: sorted(names) for n, names in by_number.items() if len(names) > 1}


def test_migration_history_has_a_single_head():
    heads = _heads(MIGRATIONS)
    assert len(heads) == 1, (
        f"Alembic has {len(heads)} heads: {sorted(heads)}. Two branches each added "
        "a migration off the same parent. Renumber the later one and point its "
        "down_revision at the other."
    )
    assert heads == pave_tenant_db.alembic_heads()


def test_no_two_migration_files_share_a_number():
    shared = _shared_numbers(MIGRATIONS / "versions")
    assert not shared, (
        f"Migration numbers used twice: {shared}. Renumber the later file "
        "(and its revision id) to the next free number."
    )


def test_the_guard_turns_red_on_a_forked_history(tmp_path):
    """Falsifier: a copy of the real history plus one sibling of the current
    head must read as two heads and one shared number."""
    copy = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS, copy, ignore=shutil.ignore_patterns("__pycache__"))
    (head,) = _heads(copy)
    number = _NUMBER.match(head).group(1)
    sibling = f"{int(number) + 1}_sibling_fork"
    twin = f"{int(number) + 1}_twin_fork"
    for rev in (sibling, twin):
        (copy / "versions" / f"{rev}.py").write_text(
            f'revision = "{rev}"\ndown_revision = "{head}"\n'
            "branch_labels = None\ndepends_on = None\n\n"
            "def upgrade():\n    pass\n\n\ndef downgrade():\n    pass\n"
        )
    assert _heads(copy) == {sibling, twin}
    assert str(int(number) + 1) in _shared_numbers(copy / "versions")
