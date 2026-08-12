"""SS-32 — SPIFFE / SPIRE workload-identity readiness layer.

This package provides parse + validation utilities for SPIFFE IDs,
X.509-SVID and JWT-SVID verification, a SPIRE trust-bundle fetcher with
TTL + stale-serve semantics, a glob-based workload capability map, and a
Starlette middleware that additively accepts SPIFFE-attested requests
alongside the existing Bearer auth.

Wiring status: ``SPIFFEAuthMiddleware`` IS mounted, in
``gdx_dispatch.app.create_app``, but only when the ``SPIFFE_ENABLE`` env var is
set to 1/true/yes — it is off by default, so SS-32 stays additive
readiness in practice. ``gdx_dispatch.core.auth_dispatcher`` also consumes this
package for its SPIFFE JWT/mTLS dispatch paths.

The super-admin router for inspecting + managing workload registrations
was never built — there is no ``gdx_dispatch.routers.spiffe_admin``.
"""
