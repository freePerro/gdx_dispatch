"""SS-32 slice D tests — workload capability map."""
from __future__ import annotations

from gdx_dispatch.core.spiffe.workload_capability_map import (
    CapabilityGrant,
    WorkloadCapabilityMap,
    resolve_capabilities,
    reset_default_map_for_tests,
)


def _map(entries):
    return WorkloadCapabilityMap.from_dict({"entries": entries})


def test_deny_by_default():
    m = _map([])
    r = m.resolve("spiffe://td.example/x")
    assert r.capabilities == ()
    assert r.matched_globs == ()
    assert r.tenant_scope == "per-tenant"


def test_invalid_spiffe_id_denied():
    m = _map([
        {"spiffe_id_glob": "spiffe://td.example/**", "capabilities": ["x"]},
    ])
    r = m.resolve("not-a-spiffe-id")
    assert r.capabilities == ()


def test_single_star_single_segment_only():
    m = _map([
        {"spiffe_id_glob": "spiffe://td.example/agent/*", "capabilities": ["mcp"]},
    ])
    # single-segment match
    r = m.resolve("spiffe://td.example/agent/worker1")
    assert "mcp" in r.capabilities
    # multi-segment should NOT match
    r = m.resolve("spiffe://td.example/agent/sub/worker1")
    assert r.capabilities == ()


def test_double_star_matches_multiple_segments():
    m = _map([
        {"spiffe_id_glob": "spiffe://td.example/agent/**", "capabilities": ["mcp"]},
    ])
    r = m.resolve("spiffe://td.example/agent/worker/1")
    assert "mcp" in r.capabilities


def test_double_star_requires_at_least_one_char():
    m = _map([
        {"spiffe_id_glob": "spiffe://td.example/agent/**", "capabilities": ["mcp"]},
    ])
    # "agent" with no suffix: not a match (** requires one+ chars)
    r = m.resolve("spiffe://td.example/agent")
    assert r.capabilities == ()


def test_union_on_multiple_matches():
    m = _map([
        {"spiffe_id_glob": "spiffe://td.example/**", "capabilities": ["a"]},
        {"spiffe_id_glob": "spiffe://td.example/agent/**", "capabilities": ["b"]},
    ])
    r = m.resolve("spiffe://td.example/agent/w1")
    assert set(r.capabilities) == {"a", "b"}
    assert set(r.matched_globs) == {
        "spiffe://td.example/**",
        "spiffe://td.example/agent/**",
    }


def test_global_scope_wins_on_union():
    m = _map([
        {
            "spiffe_id_glob": "spiffe://td.example/**",
            "capabilities": ["x"],
            "tenant_scope": "per-tenant",
        },
        {
            "spiffe_id_glob": "spiffe://td.example/system/**",
            "capabilities": ["y"],
            "tenant_scope": "global",
        },
    ])
    r = m.resolve("spiffe://td.example/system/drain")
    assert r.tenant_scope == "global"


def test_overlay_appends():
    m = _map([
        {"spiffe_id_glob": "spiffe://td.example/a", "capabilities": ["x"]},
    ])
    m.overlay([
        CapabilityGrant(
            spiffe_id_glob="spiffe://td.example/b",
            capabilities=("y",),
            tenant_scope="per-tenant",
        ),
    ])
    assert m.resolve("spiffe://td.example/b").capabilities == ("y",)
    assert m.resolve("spiffe://td.example/a").capabilities == ("x",)


def test_from_json_file_loads_defaults():
    reset_default_map_for_tests()
    r = resolve_capabilities("spiffe://example.com/agent/worker/1")
    assert "mcp:invoke" in r.capabilities


def test_list_grants_returns_snapshot():
    m = _map([
        {"spiffe_id_glob": "spiffe://td.example/a", "capabilities": ["x"]},
    ])
    grants = m.list_grants()
    assert len(grants) == 1
    assert grants[0].spiffe_id_glob == "spiffe://td.example/a"
