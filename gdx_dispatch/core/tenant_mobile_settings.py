"""Per-tenant tech-mobile feature setting reads.

Sprint tech_mobile S1-Z3.

The catalog (gdx_dispatch/core/feature_defaults.TECH_MOBILE_SETTINGS) is the
source of truth for defaults. The tenant-plane AppSettings row holds a
``tenant_mobile_settings`` JSON dict that stores **only overrides** —
keys the tenant has explicitly changed. Reading a setting falls back to
the catalog default when the override is missing.

Two access shapes:

- ``load_tenant_mobile_settings(db, request=None)`` returns the full
  resolved dict (catalog defaults merged with tenant overrides). Cached
  on ``request.state.mobile_settings`` for the lifetime of the request
  so multiple calls within a single endpoint share one DB hit.

- ``get_tenant_mobile_setting(db, key, default=_UNSET, request=None)``
  is the single-key convenience. Default-arg semantics: when ``default``
  is not provided, the catalog default wins; when ``default`` is
  provided, it overrides the catalog default ONLY if the catalog has no
  entry for ``key`` (defensive — keeps callers honest, since a key not
  in the catalog is almost always a bug).

The AppSettings row may not exist on a fresh tenant DB until first save,
so ``load_tenant_mobile_settings`` tolerates a missing row by returning
the pure-catalog defaults.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from gdx_dispatch.core.feature_defaults import TECH_MOBILE_SETTINGS
from gdx_dispatch.models.tenant_models import AppSettings


_UNSET = object()


def _catalog_defaults() -> dict[str, Any]:
    return {key: meta["default"] for key, meta in TECH_MOBILE_SETTINGS.items()}


def _read_overrides(db: Session) -> dict[str, Any]:
    row = db.query(AppSettings).first()
    if row is None:
        return {}
    overrides = row.tenant_mobile_settings or {}
    if not isinstance(overrides, dict):
        # Defensive: a non-dict here means corrupted storage. Treat as no
        # overrides rather than crashing — callers still get sane defaults.
        return {}
    return overrides


def load_tenant_mobile_settings(
    db: Session,
    request: Any = None,
) -> dict[str, Any]:
    """Return the resolved settings dict (catalog defaults + tenant overrides).

    When ``request`` is provided, the result is memoized on
    ``request.state.mobile_settings`` for the request's lifetime.
    """
    if request is not None:
        cached = getattr(request.state, "mobile_settings", None)
        if cached is not None:
            return cached

    resolved = _catalog_defaults()
    overrides = _read_overrides(db)
    # Only apply overrides whose key is in the catalog; ignore stale keys
    # left behind by prior catalog entries that have been removed. This
    # keeps reads stable across catalog revisions.
    for key, value in overrides.items():
        if key in resolved:
            resolved[key] = value

    if request is not None:
        request.state.mobile_settings = resolved

    return resolved


def get_tenant_mobile_setting(
    db: Session,
    key: str,
    default: Any = _UNSET,
    request: Any = None,
) -> Any:
    """Read a single tech-mobile setting for the current tenant.

    Resolution order:
      1. tenant override (AppSettings.tenant_mobile_settings[key])
      2. catalog default (TECH_MOBILE_SETTINGS[key]['default'])
      3. caller-supplied ``default`` (only if neither 1 nor 2 yields a value)
    """
    settings = load_tenant_mobile_settings(db, request=request)
    if key in settings:
        return settings[key]
    if default is not _UNSET:
        return default
    raise KeyError(f"unknown tech-mobile setting: {key!r}")
