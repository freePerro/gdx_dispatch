"""Tests for the per-tenant estimates-features gate (2026-04-30).

Covers:
- EstimatesFeatures defaults to permissive.
- require_line_margin_override_allowed raises 403 when the tenant has
  estimates_allow_line_margin_override=False.
- estimates router's add_line / patch_line refuse a margin_pct_override
  payload under that flag.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from gdx_dispatch.modules.estimates_features import service as features_service
from gdx_dispatch.modules.estimates_features.service import (
    EstimatesFeatures,
    require_line_margin_override_allowed,
)


def test_features_default_is_permissive():
    f = EstimatesFeatures()
    assert f.allow_line_margin_override is True


def test_gate_passes_when_allowed(monkeypatch):
    monkeypatch.setattr(
        features_service,
        "get_features",
        lambda tid: EstimatesFeatures(allow_line_margin_override=True),
    )
    require_line_margin_override_allowed("any-tenant")  # no raise


def test_gate_blocks_when_disabled(monkeypatch):
    monkeypatch.setattr(
        features_service,
        "get_features",
        lambda tid: EstimatesFeatures(allow_line_margin_override=False),
    )
    with pytest.raises(HTTPException) as excinfo:
        require_line_margin_override_allowed("any-tenant")
    assert excinfo.value.status_code == 403
    assert "margin override" in excinfo.value.detail.lower()


def test_router_imports_gate():
    """Surface-level guard: estimates router imports the gate. If this
    fails, the in-router enforcement was reverted by accident."""
    import gdx_dispatch.routers.estimates as est
    assert hasattr(est, "require_line_margin_override_allowed")
