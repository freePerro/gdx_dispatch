"""Contract tests for the comment-drift scanner.

The scanner's whole value is precision: a detector that cries wolf on
historical notes and third-party names gets switched off, and then the
real stale pointers ride along unnoticed. These tests pin both halves —
it must catch genuine drift AND stay quiet on the four shapes that
dominated the false positives during the 2026-08-12 comment audit.
"""
from __future__ import annotations

import textwrap

import pytest

from gdx_dispatch.tools.comment_drift_scan import scan


def _mkrepo(tmp_path, files: dict[str, str]):
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip("\n"))
    return tmp_path


def _dets(findings):
    return {(f["det"], f["file"]) for f in findings}


# ── it catches real drift ────────────────────────────────────────────────


def test_d1_flags_symbol_that_exists_nowhere(tmp_path):
    repo = _mkrepo(tmp_path, {
        "pkg/mod.py": '''
            # The heavy lifting happens in reticulate_splines().
            def real_function():
                return 1
        ''',
    })
    found = scan(repo, ["D1"])
    assert any("reticulate_splines" in f["detail"] for f in found)


def test_d2_flags_documented_param_the_signature_lacks(tmp_path):
    repo = _mkrepo(tmp_path, {
        "pkg/mod.py": '''
            def f(alpha):
                """Do a thing.

                Args:
                    alpha: the real one.
                    beta: removed in a refactor but still documented.
                """
                return alpha
        ''',
    })
    found = scan(repo, ["D2"])
    assert len(found) == 1
    assert "beta" in found[0]["detail"]


def test_d3_flags_docstring_method_that_fights_the_decorator(tmp_path):
    repo = _mkrepo(tmp_path, {
        "pkg/api.py": '''
            @router.post("/widgets")
            def create_widget():
                """Handles GET /widgets for the widget list."""
                return []
        ''',
    })
    found = scan(repo, ["D3"])
    assert len(found) == 1
    assert "decorator is POST /widgets" in found[0]["detail"]


def test_x1_flags_module_path_that_no_longer_resolves(tmp_path):
    repo = _mkrepo(tmp_path, {
        "gdx_dispatch/core/live.py": '''
            """See gdx_dispatch.core.deleted_helper for the old path."""
            VALUE = 1
        ''',
    })
    found = scan(repo, ["X1"])
    assert any("deleted_helper" in f["detail"] for f in found)


def test_x2_flags_missing_file_and_line_past_eof(tmp_path):
    repo = _mkrepo(tmp_path, {
        "pkg/a.py": "# See pkg/ghost.py for details.\nX = 1\n",
        "pkg/b.py": "# Pinned by pkg/a.py:900 which is way past EOF.\nY = 1\n",
    })
    found = scan(repo, ["X2"])
    details = " ".join(f["detail"] for f in found)
    assert "ghost.py" in details
    assert "has 3 lines" in details or "pkg/a.py:900" in details


# ── it stays quiet on the shapes that are NOT drift ──────────────────────


def test_historical_removal_note_is_not_drift(tmp_path):
    """'X was removed' is an accurate record, not a broken pointer."""
    repo = _mkrepo(tmp_path, {
        "gdx_dispatch/core/live.py": '''
            # gdx_dispatch.tasks.qb_sync was a no-op stub whose helpers
            # returned None. Removed 2026-05-12.
            VALUE = 1
        ''',
    })
    assert scan(repo, ["D1", "X1", "X2"]) == []


def test_live_pointer_inside_a_historical_block_is_still_flagged(tmp_path):
    """The exemption is per-sentence, not per-block.

    Block scope was self-defeating: a corrected comment is itself written as
    history, so one "was removed" anywhere exempted every other claim in the
    same paragraph — including live pointers. Measured at the time: 9 findings
    under block scope vs 35 under sentence scope, and the difference contained
    genuine dead cross-references.
    """
    repo = _mkrepo(tmp_path, {
        "gdx_dispatch/core/live.py": '''
            # gdx_dispatch.tasks.qb_sync was a no-op stub. Removed 2026-05-12.
            # Patterns mirror gdx_dispatch/models/platform.py for the new path.
            VALUE = 1
        ''',
    })
    found = scan(repo, ["X1", "X2"])
    details = " ".join(f["detail"] for f in found)
    assert "qb_sync" not in details, "the historical sentence must stay exempt"
    assert "platform.py" in details, "the live pointer beside it must be flagged"


def test_third_party_vocabulary_is_not_drift(tmp_path):
    repo = _mkrepo(tmp_path, {
        "pkg/pdf.py": '''
            # WeasyPrint renders it; a DataError from psycopg means bad input.
            # Confirm after Stripe.js completes.
            VALUE = 1
        ''',
    })
    assert scan(repo, ["D1", "X2"]) == []


def test_prose_section_after_args_is_not_a_ghost_param(tmp_path):
    """A 'Precedence:' paragraph under Args: must not read as a parameter."""
    repo = _mkrepo(tmp_path, {
        "pkg/mod.py": '''
            def f(alpha):
                """Do a thing.

                Args:
                    alpha: the real one.

                Precedence: terminals first; money beats work.
                """
                return alpha
        ''',
    })
    assert scan(repo, ["D2"]) == []


def test_negated_route_prose_is_not_method_drift(tmp_path):
    """'an SPA can't GET /start' explains the POST; it does not claim GET."""
    repo = _mkrepo(tmp_path, {
        "pkg/api.py": '''
            @router.post("/start")
            def start():
                """Mint a consent URL.

                Browsers can't carry a Bearer header on navigation, so an
                SPA can't GET /start directly.
                """
                return {}
        ''',
    })
    assert scan(repo, ["D3"]) == []


def test_usage_example_paths_are_not_drift(tmp_path):
    """`--json /tmp/out.json` and `<dbname>_data.sql` are shapes, not files."""
    repo = _mkrepo(tmp_path, {
        "pkg/tool.py": '''
            # Usage: python3 pkg/tool.py --json /tmp/out.json
            # 1. Dump data to /tmp/<dbname>_data.sql
            VALUE = 1
        ''',
    })
    assert scan(repo, ["X2"]) == []


def test_json_suffix_is_not_read_as_a_dot_js_file(tmp_path):
    """Regression: `.json` used to match the `.js` alternation."""
    repo = _mkrepo(tmp_path, {
        "pkg/tool.py": "# Writes report.json next to the source.\nVALUE = 1\n",
    })
    assert scan(repo, ["X2"]) == []


def test_module_symbol_assigned_inside_try_still_resolves(tmp_path):
    """`_FERNET = ...` inside a try: is still a module-level symbol."""
    repo = _mkrepo(tmp_path, {
        "gdx_dispatch/core/pii.py": '''
            try:
                _FERNET = object()
            except Exception:
                _FERNET = None
        ''',
        "gdx_dispatch/core/user.py": '''
            """Falls back when gdx_dispatch.core.pii._FERNET is None."""
            VALUE = 1
        ''',
    })
    assert scan(repo, ["X1"]) == []


def test_reexported_symbol_resolves_through_package_init(tmp_path):
    repo = _mkrepo(tmp_path, {
        "gdx_dispatch/routers/auth/__init__.py": "from .core import get_current_user\n",
        "gdx_dispatch/routers/auth/core.py": "def get_current_user():\n    return None\n",
        "gdx_dispatch/core/x.py": '''
            """Wraps gdx_dispatch.routers.auth.get_current_user."""
            VALUE = 1
        ''',
    })
    assert scan(repo, ["X1"]) == []


# ── plumbing ─────────────────────────────────────────────────────────────


def test_path_filter_scopes_the_scan(tmp_path):
    repo = _mkrepo(tmp_path, {
        "alpha/a.py": "# calls vanished_helper()\nX = 1\n",
        "beta/b.py": "# calls other_vanished_helper()\nY = 1\n",
    })
    found = scan(repo, ["D1"], path_filter="alpha/")
    assert found and all(f["file"].startswith("alpha/") for f in found)


def test_tests_are_excluded_unless_requested(tmp_path):
    repo = _mkrepo(tmp_path, {
        "gdx_dispatch/tests/test_x.py": "# calls vanished_helper()\nX = 1\n",
    })
    assert scan(repo, ["D1"]) == []
    assert scan(repo, ["D1"], include_tests=True)


@pytest.mark.parametrize("det", ["D1", "D2", "D3", "X1", "X2"])
def test_clean_tree_reports_nothing(tmp_path, det):
    repo = _mkrepo(tmp_path, {
        "pkg/mod.py": '''
            def helper(alpha):
                """Return alpha.

                Args:
                    alpha: the input.
                """
                return alpha
        ''',
    })
    assert scan(repo, [det]) == []
