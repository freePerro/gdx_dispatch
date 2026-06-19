"""Sprint 1.x-S25 — llm module in canonical catalog."""
from __future__ import annotations


def test_llm_module_present():
    from gdx_dispatch.core.modules import MODULES
    assert "llm" in MODULES
    entry = MODULES["llm"]
    assert "name" in entry
    assert "tier" in entry
    assert "default" in entry


def test_llm_default_is_off():
    """AI Assistant is opt-in. New tenants don't get it without explicit toggle."""
    from gdx_dispatch.core.modules import MODULES
    assert MODULES["llm"]["default"] is False


def test_llm_tier_is_starter():
    """AI features available on all tiers; module-grant + key are the actual gates.

    Originally specced as tier=business but lowered to starter 2026-04-27 after
    deploy-time discovery: GDX (the design partner) ships as subscription_status=
    trialing → fallback tier=starter, so business-gated modules were unreachable
    on the proving-ground tenant. The real gate is the tenant_module_grants row
    presence + the per-tenant Anthropic key (TenantSettings.llm_provider_key_enc).
    """
    from gdx_dispatch.core.modules import MODULES
    assert MODULES["llm"]["tier"] == "starter"
