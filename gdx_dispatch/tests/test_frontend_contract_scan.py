"""Contract tests for the frontend↔backend contract scanner.

Both directions matter. A scanner that misses the Stripe-card bug is
useless; one that flags every `${qs}` query string gets switched off, and
then it misses the Stripe-card bug anyway. These pin the real detections
and the false-positive classes that showed up on the first live run.
"""
from __future__ import annotations

import textwrap

import pytest

from gdx_dispatch.tools.frontend_contract_scan import (
    normalize,
    path_matches,
    scan,
)


def _mkrepo(tmp_path, files: dict[str, str]):
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip("\n"))
    return tmp_path


def _checks(findings):
    return [f["check"] for f in findings]


# ── normalisation ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/api/jobs/{job_id}/financials", "/api/jobs/{}/financials"),
        ("/api/jobs/${id}/financials", "/api/jobs/{}/financials"),
        ("/api/payments?x=1", "/api/payments"),
        # a ${var} NOT after a slash is a query/suffix, not a path segment
        ("/api/payments${qs}", "/api/payments"),
        ("/api/reports/summary${params}", "/api/reports/summary"),
        ("/api/door-listings${qs}", "/api/door-listings"),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_path_matches_treats_either_side_wildcard():
    # the Vue computes a whole segment: /api/jobs/${id}/${target}
    assert path_matches("/api/jobs/{}/{}", "/api/jobs/{}/reactivate")
    assert path_matches("/api/jobs/{}/parts", "/api/jobs/{}/parts")
    assert not path_matches("/api/jobs/{}", "/api/jobs/{}/parts")
    assert not path_matches("/api/stripe-connect/onboard", "/api/stripe/connect/onboard")


# ── C1 / C2: it catches real drift ───────────────────────────────────────


def test_c1_flags_call_with_no_route(tmp_path):
    repo = _mkrepo(tmp_path, {
        "gdx_dispatch/frontend/src/views/X.vue": """
            const r = await api.post('/api/stripe-connect/onboard');
        """,
    })
    found = scan(repo, ["C1"], routes=[{"method": "POST", "path": "/api/stripe/connect/onboard"}])
    assert _checks(found) == ["C1"]
    assert "stripe-connect" in found[0]["detail"]


def test_c2_flags_method_mismatch(tmp_path):
    """The equipment bug: FE PATCHes a path the backend only serves for PUT."""
    repo = _mkrepo(tmp_path, {
        "gdx_dispatch/frontend/src/views/Equip.vue": """
            await api.patch(`/api/equipment/${form.value.id}`, payload);
        """,
    })
    found = scan(repo, ["C2"], routes=[{"method": "PUT", "path": "/api/equipment/{equipment_id}"}])
    assert _checks(found) == ["C2"]
    assert "405" in found[0]["detail"]


def test_query_string_call_is_not_reported_dead(tmp_path):
    repo = _mkrepo(tmp_path, {
        "gdx_dispatch/frontend/src/views/Pay.vue": """
            const r = await api.get(`/api/payments${qs}`);
        """,
    })
    assert scan(repo, ["C1", "C2"], routes=[{"method": "GET", "path": "/api/payments"}]) == []


def test_dynamic_segment_call_is_not_reported_dead(tmp_path):
    repo = _mkrepo(tmp_path, {
        "gdx_dispatch/frontend/src/views/J.vue": """
            await api.post(`/api/jobs/${id}/${target}`, {});
        """,
    })
    routes = [{"method": "POST", "path": "/api/jobs/{job_id}/reactivate"}]
    assert scan(repo, ["C1", "C2"], routes=routes) == []


def test_scaffolding_and_test_fixtures_are_ignored(tmp_path):
    repo = _mkrepo(tmp_path, {
        "gdx_dispatch/frontend/src/views/_ViewTemplate.vue": "await api.get('/api/my-feature');",
        "gdx_dispatch/frontend/tests/api.test.js": "await api.del('/api/nope/abc');",
        "gdx_dispatch/frontend/src/views/__tests__/A.spec.js": "await api.get('/api/nope');",
    })
    assert scan(repo, ["C1", "C2"], routes=[]) == []


def test_comments_are_not_call_sites(tmp_path):
    """useApi's own docstring shows `api.del('/api/foo/${id}')` as an example."""
    repo = _mkrepo(tmp_path, {
        "gdx_dispatch/frontend/src/composables/useApi.js": """
            /**
             *   await api.del(`/api/foo/${id}`, { successMessage: 'x' });
             *   const data = await api.get('/api/list');
             */
            export function useApi() {}
        """,
    })
    assert scan(repo, ["C1", "C2"], routes=[]) == []


# ── C3: phantom response fields ──────────────────────────────────────────


def test_c3_flags_field_the_handler_cannot_return(tmp_path):
    """The actual Settings bug: reads result.stripe; handler never returns it."""
    repo = _mkrepo(tmp_path, {
        "gdx_dispatch/routers/settings.py": """
            router = APIRouter(prefix="/api/settings")

            @router.get("/integrations")
            def list_integrations():
                return {"integrations": {}, "google_maps": {}}
        """,
        "gdx_dispatch/frontend/src/views/S.vue": """
            async function loadIntegrations() {
              const result = await api.get("/api/settings/integrations");
              if (result?.stripe) integrations.stripe = result.stripe;
            }
        """,
    })
    found = [f for f in scan(repo, ["C3"]) if f["check"] == "C3"]
    assert found, "should flag the phantom `stripe` field"
    assert "stripe" in found[0]["detail"]


def test_c3_respects_function_scope(tmp_path):
    """A `const response` in the NEXT function must not be attributed here.

    This was a real false positive: PhoneComIntegrationCard declares
    `const response` in two adjacent functions hitting different endpoints.
    """
    repo = _mkrepo(tmp_path, {
        "gdx_dispatch/routers/s.py": """
            router = APIRouter(prefix="/api/settings")

            @router.get("/modules")
            def modules():
                return {"modules": []}

            @router.get("/integrations/phone-com")
            def phone():
                return {"voip_id": 1}
        """,
        "gdx_dispatch/frontend/src/components/P.vue": """
            const fetchModules = async () => {
              const response = await api.get('/api/settings/modules')
              const mod = response.modules
            }

            const fetchSettings = async () => {
              const response = await api.get('/api/settings/integrations/phone-com')
              voipIdInput.value = response.voip_id
            }
        """,
    })
    assert [f for f in scan(repo, ["C3"]) if f["check"] == "C3"] == []


def test_c3_skips_handlers_with_unknowable_shape(tmp_path):
    """response_model or a non-literal return means the keys aren't knowable."""
    repo = _mkrepo(tmp_path, {
        "gdx_dispatch/routers/r.py": """
            router = APIRouter(prefix="/api")

            @router.get("/thing", response_model=Thing)
            def thing():
                return {"a": 1}
        """,
        "gdx_dispatch/frontend/src/views/T.vue": """
            const r = await api.get('/api/thing');
            const x = r.anything;
        """,
    })
    assert [f for f in scan(repo, ["C3"]) if f["check"] == "C3"] == []


# ── C5: stub endpoints ───────────────────────────────────────────────────


def test_c5_flags_handler_that_only_returns_blanks(tmp_path):
    repo = _mkrepo(tmp_path, {
        "gdx_dispatch/routers/ui_compat.py": """
            router = APIRouter()

            @router.get("/api/loyalty")
            def loyalty_index():
                return {"members": [], "redemptions": [], "tiers": []}
        """,
    })
    found = [f for f in scan(repo, ["C5"]) if f["check"] == "C5"]
    assert len(found) == 1
    assert "/api/loyalty" in found[0]["detail"]


def test_c5_does_not_flag_a_delete_that_does_work(tmp_path):
    """`return {}` after real work is not a stub — the body must be empty."""
    repo = _mkrepo(tmp_path, {
        "gdx_dispatch/routers/r.py": """
            router = APIRouter()

            @router.delete("/api/thing/{thing_id}")
            def delete_thing(thing_id):
                db.delete(thing_id)
                db.commit()
                return {}
        """,
    })
    assert [f for f in scan(repo, ["C5"]) if f["check"] == "C5"] == []


# ── C6: mutations that report success without doing anything ─────────────


def test_c6_flags_mutation_returning_a_sentinel_helper(tmp_path):
    """The `ui_compat` shape: `return _ok()` answering a PATCH."""
    repo = _mkrepo(tmp_path, {
        "gdx_dispatch/routers/ui_compat.py": """
            router = APIRouter()

            def _ok():
                return {"ok": True}

            @router.patch("/api/pricing/{entry_id}")
            def update_pricing_entry(entry_id, payload):
                return _ok()
        """,
    })
    found = [f for f in scan(repo, ["C6"]) if f["check"] == "C6"]
    assert len(found) == 1
    assert "/api/pricing/{entry_id}" in found[0]["detail"]


def test_c6_does_not_flag_a_thin_controller(tmp_path):
    """`return add_proposal_tier(...)` delegates real work to a service.

    A single-return body is NOT a no-op when the return calls something with
    arguments — that is the ordinary thin-controller pattern. Without this
    exclusion C6 reported every delegating handler in the codebase.
    """
    repo = _mkrepo(tmp_path, {
        "gdx_dispatch/modules/proposals/router.py": """
            router = APIRouter()

            @router.post("/estimates/{estimate_id}/proposal-tiers")
            def post_proposal_tier(estimate_id, payload, db):
                return add_proposal_tier(estimate_id, payload.tier_name, db)
        """,
    })
    assert [f for f in scan(repo, ["C6"]) if f["check"] == "C6"] == []


def test_c6_ignores_read_routes(tmp_path):
    """A GET that returns a literal is C5's business, not C6's."""
    repo = _mkrepo(tmp_path, {
        "gdx_dispatch/routers/r.py": """
            router = APIRouter()

            @router.get("/api/thing")
            def thing():
                return {"items": []}
        """,
    })
    checks = {f["check"] for f in scan(repo, ["C5", "C6"])}
    assert checks == {"C5"}


def test_c5_c6_skip_handlers_that_lose_route_arbitration(tmp_path):
    """A no-op shadowed by a real handler is dead code, not a live bug.

    Only applies when a live route table names the winning endpoint.
    """
    repo = _mkrepo(tmp_path, {
        "gdx_dispatch/routers/ui_compat.py": """
            router = APIRouter()

            def _ok():
                return {"ok": True}

            @router.patch("/api/thing/{thing_id}")
            def update_thing(thing_id):
                return _ok()
        """,
    })
    routes = [{
        "method": "PATCH", "path": "/api/thing/{thing_id}",
        "endpoint": "gdx_dispatch.routers.real.update_thing",   # a different winner
    }]
    assert [f for f in scan(repo, ["C6"], routes=routes) if f["check"] == "C6"] == []

    routes[0]["endpoint"] = "gdx_dispatch.routers.ui_compat.update_thing"  # no-op wins
    assert [f for f in scan(repo, ["C6"], routes=routes) if f["check"] == "C6"]


# ── plumbing ─────────────────────────────────────────────────────────────


def test_clean_pair_reports_nothing(tmp_path):
    repo = _mkrepo(tmp_path, {
        "gdx_dispatch/routers/r.py": """
            router = APIRouter(prefix="/api")

            @router.get("/jobs/{job_id}")
            def get_job(job_id):
                return {"id": job_id, "title": "x"}
        """,
        "gdx_dispatch/frontend/src/views/J.vue": """
            const j = await api.get(`/api/jobs/${id}`);
            title.value = j.title;
        """,
    })
    assert scan(repo, ["C1", "C2", "C3", "C5"]) == []
