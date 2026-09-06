"""Slice outlook-s4 — verify the email module is registered."""
from __future__ import annotations

from gdx_dispatch.core.modules import MODULES


def test_email_module_present():
    assert "email" in MODULES, "email module must be registered in MODULES"


def test_email_module_shape_matches_other_integrations():
    entry = MODULES["email"]
    assert entry["name"] == "Email Integration"
    assert set(entry) == {"name"}, "plan tiers and the default flag left the catalog 2026-09-06"


def test_email_is_not_an_alias_target():
    """The new module key must not appear in LEGACY_MODULE_ALIASES (it's brand new)."""
    from gdx_dispatch.core.modules import LEGACY_MODULE_ALIASES
    assert "email" not in LEGACY_MODULE_ALIASES.values(), \
        "email is brand new — should not be an alias target"
    assert "email" not in LEGACY_MODULE_ALIASES, \
        "email key must not be aliased to anything else"
