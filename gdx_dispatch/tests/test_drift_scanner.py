"""Regression tests for gdx_dispatch/tools/drift_scanner.py silent-except checker.

2026-04-17 — filed after a false-positive on gdx_dispatch/routers/auth.py sat in
the bug queue firing CRITICAL health alerts. Root cause: the silent-except
AST check flagged any except block without a literal log/raise inside
its body, missing the capture-and-reuse pattern used by fallback-auth
paths (capture into a variable, then log at a tail handler with both
path errors together).

These tests pin the expected acceptance set so the same class of false
positive cannot regress.
"""
from __future__ import annotations

import importlib
import os
import textwrap

import pytest


@pytest.fixture
def scanner(monkeypatch, tmp_path):
    """Import drift_scanner with REPO_ROOT + scan dirs redirected to tmp.

    Also resets the module-level `violations` list so cross-test state
    does not bleed.
    """
    mod = importlib.import_module("gdx_dispatch.tools.drift_scanner")
    # Redirect scan roots to tmp
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(mod, "GDX_DIR", tmp_path / "gdx_dispatch", raising=True)
    monkeypatch.setattr(mod, "APP_PY", tmp_path / "gdx_dispatch" / "app.py", raising=True)
    monkeypatch.setattr(mod, "ROUTERS_DIR", tmp_path / "gdx_dispatch" / "routers", raising=True)
    monkeypatch.setattr(mod, "CORE_DIR", tmp_path / "gdx_dispatch" / "core", raising=True)
    (tmp_path / "gdx_dispatch" / "routers").mkdir(parents=True)
    (tmp_path / "gdx_dispatch" / "core").mkdir(parents=True)
    (tmp_path / "gdx_dispatch" / "modules").mkdir(parents=True)
    mod.violations = []
    return mod, tmp_path


def _write(tmp: os.PathLike, rel: str, src: str) -> None:
    from pathlib import Path
    p = Path(tmp) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(src), encoding="utf-8")


def test_bare_except_pass_is_flagged(scanner):
    mod, tmp = scanner
    _write(tmp, "gdx_dispatch/routers/bare.py", """
        def f():
            try:
                risky()
            except:
                pass
    """)
    mod.check_silent_exceptions()
    assert any("bare.py:" in v for v in mod.violations)


def test_except_named_pass_is_flagged(scanner):
    mod, tmp = scanner
    _write(tmp, "gdx_dispatch/routers/named_pass.py", """
        def f():
            try:
                risky()
            except ValueError:
                pass
    """)
    mod.check_silent_exceptions()
    assert any("named_pass.py:" in v for v in mod.violations)


def test_except_with_logger_warn_is_not_flagged(scanner):
    mod, tmp = scanner
    _write(tmp, "gdx_dispatch/routers/logged.py", """
        import logging
        logger = logging.getLogger(__name__)

        def f():
            try:
                risky()
            except ValueError as exc:
                logger.warning("bad: %s", exc)
    """)
    mod.check_silent_exceptions()
    assert not any("logged.py:" in v for v in mod.violations)


def test_except_with_raise_is_not_flagged(scanner):
    mod, tmp = scanner
    _write(tmp, "gdx_dispatch/routers/reraise.py", """
        def f():
            try:
                risky()
            except ValueError:
                raise
    """)
    mod.check_silent_exceptions()
    assert not any("reraise.py:" in v for v in mod.violations)


def test_except_capture_and_reuse_is_not_flagged(scanner):
    """2026-04-17 regression — the fallback-auth pattern.

    The except body stores the captured error into another variable
    that the enclosing function uses later. This is intentional
    diagnostics-preserving fallback, not silent-swallow.
    """
    mod, tmp = scanner
    _write(tmp, "gdx_dispatch/routers/capture.py", """
        def f():
            primary_error = None
            try:
                primary()
            except RuntimeError as exc:
                primary_error = exc
            if primary_error is not None:
                log_later(primary_error)
    """)
    mod.check_silent_exceptions()
    assert not any("capture.py:" in v for v in mod.violations)


def test_except_passes_captured_name_to_function_is_not_flagged(scanner):
    mod, tmp = scanner
    _write(tmp, "gdx_dispatch/routers/pass_arg.py", """
        def f():
            try:
                risky()
            except RuntimeError as exc:
                queue_for_later(exc)
    """)
    mod.check_silent_exceptions()
    assert not any("pass_arg.py:" in v for v in mod.violations)


def test_except_bound_name_only_assignment_is_not_flagged(scanner):
    """Even a minimal `captured = exc` is a reuse signal."""
    mod, tmp = scanner
    _write(tmp, "gdx_dispatch/routers/minimal_capture.py", """
        def f():
            captured = None
            try:
                risky()
            except RuntimeError as exc:
                captured = exc
            return captured
    """)
    mod.check_silent_exceptions()
    assert not any("minimal_capture.py:" in v for v in mod.violations)


def test_except_named_but_not_referenced_is_flagged(scanner):
    """`except X as exc:` with a body that never touches `exc` is still
    silent-swallow — the alias alone doesn't save the error."""
    mod, tmp = scanner
    _write(tmp, "gdx_dispatch/routers/named_not_used.py", """
        def f():
            try:
                risky()
            except ValueError as exc:
                do_unrelated_cleanup()
    """)
    mod.check_silent_exceptions()
    assert any("named_not_used.py:" in v for v in mod.violations)


# ── check_import_logging regression: widened window + noqa respect ─────────

def test_import_logging_finds_log_four_lines_past_except(scanner):
    """2026-04-17 regression — app.py:1186 pattern. except + 3-line
    comment block + logging call on line 4 past the except. Old 3-line
    window missed this."""
    mod, tmp = scanner
    _write(tmp, "gdx_dispatch/app.py", """
        def _probe():
            try:
                risky()
                return True
            except Exception:
                # Health-probe pattern: False return IS the signal.
                # Not a silent swallow — information reaches operator
                # via the probe result.
                logging.getLogger("gdx_dispatch.app").exception("probe failed")
                return False
    """)
    mod.check_import_logging()
    assert not any("app.py" in v for v in mod.violations)


def test_import_logging_respects_noqa_silent_failure_marker(scanner):
    """noqa: silent-failure marker on the except line should skip."""
    mod, tmp = scanner
    _write(tmp, "gdx_dispatch/app.py", """
        def f():
            try:
                risky()
            except Exception:  # noqa: silent-failure
                pass
    """)
    mod.check_import_logging()
    assert not any("app.py" in v for v in mod.violations)


def test_import_logging_flags_genuine_silent_swallow(scanner):
    """Positive control — an except with no log/raise within 10 lines
    and no noqa marker still gets flagged."""
    mod, tmp = scanner
    _write(tmp, "gdx_dispatch/app.py", """
        def f():
            try:
                risky()
            except Exception:
                x = 1
                y = 2
                z = 3
                # nothing logs here
                return None
    """)
    mod.check_import_logging()
    assert any("app.py" in v for v in mod.violations)


def test_import_logging_substring_match_no_longer_false_positive(scanner):
    """Old code matched 'error' anywhere in the next 3 lines, including
    in unrelated identifiers like `validation_error`. New code requires
    a method-call-shaped match like `.exception(` or bare `raise`."""
    mod, tmp = scanner
    _write(tmp, "gdx_dispatch/app.py", """
        def f():
            try:
                risky()
            except Exception:
                # This has validation_error in a comment but no logging
                return default_validation_error_response
    """)
    mod.check_import_logging()
    # Old regex would have matched "error" → no violation. New regex
    # requires the method-call shape → violation (correct).
    assert any("app.py" in v for v in mod.violations)


def test_real_world_fallback_auth_pattern(scanner):
    """The exact gdx_dispatch/routers/auth.py:523 shape that tripped the bug
    queue on 2026-04-17. Must NOT be flagged."""
    mod, tmp = scanner
    _write(tmp, "gdx_dispatch/routers/auth.py", """
        import logging
        log = logging.getLogger(__name__)

        class JWTValidationError(Exception): pass
        class ExpiredSignatureError(Exception): pass

        def validate(token):
            primary_error = None
            try:
                return primary_validator(token)
            except JWTValidationError as exc:
                # Fall through to legacy decoder. Capture for tail log.
                primary_error = exc
            try:
                return legacy_validator(token)
            except ExpiredSignatureError:
                raise unauth("Token expired")
            except (ValueError, TypeError) as exc:
                if primary_error is not None:
                    log.warning(
                        "auth_access_token_invalid: core=%s:%s legacy=%s:%s",
                        type(primary_error).__name__,
                        str(primary_error)[:60],
                        type(exc).__name__,
                        str(exc)[:60],
                    )
                else:
                    log.warning("auth_access_token_invalid: %s: %s", type(exc).__name__, str(exc)[:120])
                raise unauth("Invalid or expired access token")
    """)
    mod.check_silent_exceptions()
    auth_violations = [v for v in mod.violations if "auth.py" in v]
    assert auth_violations == [], (
        f"Real-world fallback-auth pattern wrongly flagged as silent: {auth_violations}"
    )
