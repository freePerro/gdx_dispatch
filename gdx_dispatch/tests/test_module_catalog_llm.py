"""Sprint 1.x-S25 — llm module in canonical catalog."""
from __future__ import annotations


def test_llm_module_present():
    from gdx_dispatch.core.modules import MODULES
    assert "llm" in MODULES
    entry = MODULES["llm"]
    assert entry == {"name": "AI Assistant"}


def test_llm_carries_no_default_flag():
    """There is no per-module default any more: the seeder grants every module
    once, and an admin disable sticks (see core.modules._seed_default_modules)."""
    from gdx_dispatch.core.modules import MODULES
    assert "default" not in MODULES["llm"]


def test_no_module_carries_a_tier():
    """Plan tiers ("starter" / "professional" / "business") were the price
    ladder of a subscription this self-hosted app never sold; removed
    2026-09-06. The real gates are the company_module_grants row and, for AI,
    the per-install Anthropic key (TenantSettings.llm_provider_key_enc)."""
    from gdx_dispatch.core.modules import MODULES
    assert not [k for k, v in MODULES.items() if "tier" in v or "default" in v]
