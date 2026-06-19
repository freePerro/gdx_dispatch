"""Smoke tests for gdx_dispatch/tools/create_api_key.py CLI."""
from __future__ import annotations

import io
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

from gdx_dispatch.tools import create_api_key as cli


def test_dry_run_prints_without_writing():
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main([
            "--tenant", "gdx",
            "--name", "smoke",
            "--scopes", "landing_leads:write",
            "--dry-run",
        ])
    assert rc == 0
    out = buf.getvalue()
    assert "DRY RUN" in out
    assert "landing_leads:write" in out


def test_invalid_scope_raises_systemexit():
    with pytest.raises(SystemExit) as exc_info:
        cli.main([
            "--tenant", "gdx",
            "--name", "bad",
            "--scopes", "totally:fake:scope",
            "--dry-run",
        ])
    assert "invalid scopes" in str(exc_info.value).lower()


def test_empty_scopes_raises_systemexit():
    with pytest.raises(SystemExit) as exc_info:
        cli.main([
            "--tenant", "gdx",
            "--name", "empty",
            "--scopes", "",
            "--dry-run",
        ])
    assert "at least one scope" in str(exc_info.value).lower()


def test_resolve_tenant_id_accepts_uuid():
    """A UUID arg returns the same UUID without DB lookup."""
    fake_uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    # Pass None for db — UUID branch never touches it
    result = cli._resolve_tenant_id(None, fake_uuid)
    assert str(result) == fake_uuid


def test_resolve_tenant_id_looks_up_slug():
    """Slug arg triggers a DB query."""
    fake_uuid = "11111111-2222-3333-4444-555555555555"

    class _FakeMappings:
        def first(self):
            return {"id": fake_uuid}

    class _FakeResult:
        def mappings(self):
            return _FakeMappings()

    class _FakeDB:
        def execute(self, *args, **kwargs):
            return _FakeResult()

    result = cli._resolve_tenant_id(_FakeDB(), "gdx")
    assert str(result) == fake_uuid


def test_resolve_tenant_id_unknown_slug_exits():
    class _FakeMappings:
        def first(self):
            return None

    class _FakeResult:
        def mappings(self):
            return _FakeMappings()

    class _FakeDB:
        def execute(self, *args, **kwargs):
            return _FakeResult()

    with pytest.raises(SystemExit) as exc_info:
        cli._resolve_tenant_id(_FakeDB(), "does-not-exist")
    assert "not found" in str(exc_info.value).lower()
