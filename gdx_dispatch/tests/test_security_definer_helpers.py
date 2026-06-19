"""SS-17 slice B — unit tests for security_definer Python helpers.

sqlite-compatible. No real DEFINER functions exist here; we verify:

* allow-list enforcement (unknown function name → immediate raise)
* non-PG backend path is a safe no-op returning ``[]`` / yielding ``None``
* the PG-path SQL shape (bind params, never f-string interpolation) via
  a recording fake session
* :func:`assert_known_functions_match` catches drift in both directions
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from gdx_dispatch.core.security_definer import (
    KNOWN_DEFINER_FUNCTIONS,
    SecurityDefinerExecutionError,
    UnknownSecurityDefinerFunction,
    assert_known_functions_match,
    call_security_definer,
    with_security_definer,
)


class _FakeBind:
    def __init__(self, dialect_name: str) -> None:
        self.dialect = SimpleNamespace(name=dialect_name)


class _FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _RecordingSession:
    """Minimal Session stand-in that records execute() calls."""

    def __init__(self, dialect: str, result_rows: list[tuple[Any, ...]] | None = None,
                 raise_on_execute: Exception | None = None) -> None:
        self._bind = _FakeBind(dialect)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._result_rows = result_rows or []
        self._raise = raise_on_execute

    def get_bind(self) -> _FakeBind:
        return self._bind

    def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        # SQLAlchemy TextClause stringifies to its SQL text.
        self.calls.append((str(stmt), params or {}))
        if self._raise is not None:
            raise self._raise
        return _FakeResult(self._result_rows)


# ─────────────────────────────────────────────────────────────────────────
# allow-list
# ─────────────────────────────────────────────────────────────────────────

def test_known_set_not_empty_and_all_schema_qualified() -> None:
    assert KNOWN_DEFINER_FUNCTIONS, "KNOWN_DEFINER_FUNCTIONS must not be empty"
    for name in KNOWN_DEFINER_FUNCTIONS:
        assert "." in name, f"function name must be schema-qualified: {name!r}"


def test_call_unknown_function_raises_before_any_sql() -> None:
    s = _RecordingSession("postgresql")
    with pytest.raises(UnknownSecurityDefinerFunction):
        call_security_definer(s, "reporting.does_not_exist", "tenant-a")
    assert s.calls == [], "must not execute SQL for unknown function"


def test_context_manager_unknown_function_raises_before_enter() -> None:
    s = _RecordingSession("postgresql")
    with pytest.raises(UnknownSecurityDefinerFunction):
        with with_security_definer(s, "reporting.does_not_exist"):
            pass  # pragma: no cover
    assert s.calls == []


# ─────────────────────────────────────────────────────────────────────────
# non-PG path (sqlite dev / test env)
# ─────────────────────────────────────────────────────────────────────────

def test_call_on_sqlite_returns_empty_list_no_sql() -> None:
    s = _RecordingSession("sqlite")
    # Pick any allow-listed name.
    fn = next(iter(KNOWN_DEFINER_FUNCTIONS))
    out = call_security_definer(s, fn, "tenant-a")
    assert out == []
    assert s.calls == [], "sqlite path must not issue SQL"


def test_context_manager_on_sqlite_yields_none() -> None:
    s = _RecordingSession("sqlite")
    fn = next(iter(KNOWN_DEFINER_FUNCTIONS))
    with with_security_definer(s, fn, "tenant-a") as rows:
        assert rows is None
    assert s.calls == []


# ─────────────────────────────────────────────────────────────────────────
# PG path — SQL shape + parameter binding
# ─────────────────────────────────────────────────────────────────────────

def test_pg_path_uses_bound_params_not_interpolation() -> None:
    s = _RecordingSession("postgresql", result_rows=[("2026-04", 100)])
    fn = next(iter(KNOWN_DEFINER_FUNCTIONS))
    rows = call_security_definer(s, fn, "tenant-a", 42)
    assert rows == [("2026-04", 100)]
    assert len(s.calls) == 1
    sql, params = s.calls[0]
    # Expected shape: SELECT * FROM <fn>(:a0, :a1)
    assert sql.startswith(f"SELECT * FROM {fn}(")
    assert ":a0" in sql and ":a1" in sql
    assert params == {"a0": "tenant-a", "a1": 42}
    # Arguments must never be string-interpolated into the SQL.
    assert "tenant-a" not in sql
    assert "42" not in sql


def test_pg_path_zero_args() -> None:
    s = _RecordingSession("postgresql", result_rows=[])
    fn = next(iter(KNOWN_DEFINER_FUNCTIONS))
    out = call_security_definer(s, fn)
    assert out == []
    sql, params = s.calls[0]
    assert sql == f"SELECT * FROM {fn}()"
    assert params == {}


def test_pg_path_execute_failure_wrapped() -> None:
    boom = RuntimeError("simulated PG failure")
    s = _RecordingSession("postgresql", raise_on_execute=boom)
    fn = next(iter(KNOWN_DEFINER_FUNCTIONS))
    with pytest.raises(SecurityDefinerExecutionError) as ei:
        call_security_definer(s, fn, "x")
    assert ei.value.__cause__ is boom


def test_context_manager_pg_path_yields_rows() -> None:
    s = _RecordingSession("postgresql", result_rows=[(1,), (2,)])
    fn = next(iter(KNOWN_DEFINER_FUNCTIONS))
    with with_security_definer(s, fn, "tenant-a") as rows:
        assert rows == [(1,), (2,)]


# ─────────────────────────────────────────────────────────────────────────
# drift guard for migration / allow-list cross-check
# ─────────────────────────────────────────────────────────────────────────

def test_assert_known_functions_match_ok() -> None:
    assert_known_functions_match(list(KNOWN_DEFINER_FUNCTIONS))


def test_assert_known_functions_match_detects_missing() -> None:
    subset = list(KNOWN_DEFINER_FUNCTIONS)[:-1]
    with pytest.raises(AssertionError):
        assert_known_functions_match(subset)


def test_assert_known_functions_match_detects_extra() -> None:
    extra = list(KNOWN_DEFINER_FUNCTIONS) + ["reporting.ghost_function"]
    with pytest.raises(AssertionError):
        assert_known_functions_match(extra)
