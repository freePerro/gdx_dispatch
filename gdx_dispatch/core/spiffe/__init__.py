"""SS-32 — SPIFFE / SPIRE workload-identity readiness layer.

This package provides parse + validation utilities for SPIFFE IDs,
X.509-SVID and JWT-SVID verification, a SPIRE trust-bundle fetcher with
TTL + stale-serve semantics, a glob-based workload capability map, a
Starlette middleware that additively accepts SPIFFE-attested requests
alongside the existing Bearer auth, and a super-admin router for
inspecting + managing workload registrations.

INTEGRATION_TODO: none of these components are wired into
``gdx_dispatch/main.py`` — SS-32 is additive readiness. Mount the middleware and
include ``gdx_dispatch.routers.spiffe_admin`` when Doug flips the SPIFFE-enabled
flag at main-chain merge.
"""
