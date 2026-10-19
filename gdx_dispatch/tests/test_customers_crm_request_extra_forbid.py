"""Guard: a JSON body in a customers-crm router refuses keys it does not declare.

Pydantic's default is ``extra="ignore"``: a key the schema does not name is
dropped and the request answers 200. That is how CustomerFormDialog's ``notes``
and the location dialog's ``city`` were "saved" for months (#683), and how
SurveysView's template edit changed nothing (GDXA-1676). GDXA-1697 set
``extra="forbid"`` on every request body in these routers after a caller audit
(frontend, mobile, the server-rendered survey page, MCP tools, services,
plugins); this file keeps a new body schema from shipping on the default.

The body models are read from the routes FastAPI actually registered, not from
class names in source, so a schema imported from a module or a nested model is
covered too. The module set is every file the customers-crm rows of
``tools/agent_ownership.txt`` own that defines an ``APIRouter`` (routers/, and
core/locations.py), so a new router there is covered without editing this file.
Only routers a module defines are walked, not ones it imports.
"""
from __future__ import annotations

import importlib
import os
import re
import typing
from pathlib import Path

import pytest
from fastapi.routing import APIRoute, APIRouter
from pydantic import BaseModel, ConfigDict, ValidationError

from gdx_dispatch.tools import agent_ownership_scan as scan

REPO = Path(__file__).resolve().parents[2]

# Body schemas left on the default, each with the reason. Keyed "module:Class".
ALLOWED_IGNORE = {
    # ReferralsView PATCHes the whole row it loaded ({...referral}); only
    # `status` is read. Forbid would 422 the status edit that works today. The
    # form is deferred as product shape (GDXA-1676 sweep, GDXA-1697 item 6).
    "gdx_dispatch.routers.referrals:ReferralPatchIn": "ReferralsView sends the full row; form deferred",
    # PUT /api/leads/{id}/custom-fields reuses platform-core's schema from
    # routers/custom_fields.py, which also serves that router's own routes;
    # its config is platform-core's call, not this territory's.
    "gdx_dispatch.routers.custom_fields:CustomFieldValueUpsert": "owned by platform-core (custom_fields.py)",
    # Owned by customers-crm but outside GDXA-1697's triaged list; the
    # quote-request edit form round-trips door rows, so each needs its own
    # caller audit before forbid (deferred, counted on GDXA-1697).
    "gdx_dispatch.modules.quote_requests.service:QuoteRequestIn": "deferred: caller audit not run",
    "gdx_dispatch.modules.quote_requests.service:QuoteRequestEdit": "deferred: caller audit not run",
    "gdx_dispatch.modules.quote_requests.service:DoorIn": "deferred: caller audit not run",
    "gdx_dispatch.modules.quote_requests.service:DoorEdit": "deferred: caller audit not run",
    "gdx_dispatch.modules.reseller.service:ProfileIn": "deferred: caller audit not run",
    "gdx_dispatch.modules.reseller.service:ResaleQuoteIn": "deferred: caller audit not run",
}


_ROUTER_DEF = re.compile(r"^(\w+)\s*(?::\s*APIRouter\s*)?=\s*APIRouter\(", re.M)
_SKIP_DIRS = {"tests", "frontend", "node_modules", "__pycache__", "migrations"}


def _owned_router_modules() -> dict[str, list[str]]:
    """Module name -> the APIRouter variables it defines, for every
    customers-crm-owned file that defines one."""
    rules = scan.load_rules()
    mods: dict[str, list[str]] = {}
    for root, dirs, files in os.walk(REPO / "gdx_dispatch"):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = Path(root) / fname
            rel = path.relative_to(REPO).as_posix()
            if scan.owner_of(rel, rules) != "customers-crm":
                continue
            names = _ROUTER_DEF.findall(path.read_text(encoding="utf-8"))
            if names:
                mods[rel[: -len(".py")].replace("/", ".")] = names
    return dict(sorted(mods.items()))


def _models_in(annotation: object) -> list[type[BaseModel]]:
    """Every BaseModel subclass reachable from a type annotation."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    out: list[type[BaseModel]] = []
    for arg in typing.get_args(annotation):
        out.extend(_models_in(arg))
    return out


def _body_models(dependant) -> list[type[BaseModel]]:
    out: list[type[BaseModel]] = []
    for param in dependant.body_params:
        out.extend(_models_in(param.field_info.annotation))
    for sub in dependant.dependencies:
        out.extend(_body_models(sub))
    return out


def _with_nested(models: list[type[BaseModel]]) -> set[type[BaseModel]]:
    seen: set[type[BaseModel]] = set()
    stack = list(models)
    while stack:
        model = stack.pop()
        if model in seen:
            continue
        seen.add(model)
        for field in model.model_fields.values():
            stack.extend(_models_in(field.annotation))
    return seen


def _key(model: type[BaseModel]) -> str:
    return f"{model.__module__}:{model.__qualname__}"


@pytest.fixture(scope="module")
def body_models() -> dict[str, type[BaseModel]]:
    found: list[type[BaseModel]] = []
    for name, router_names in _owned_router_modules().items():
        module = importlib.import_module(name)
        for router_name in router_names:
            router = getattr(module, router_name)
            assert isinstance(router, APIRouter), f"{name}.{router_name}"
            for route in router.routes:
                if isinstance(route, APIRoute):
                    found.extend(_body_models(route.dependant))
    return {_key(m): m for m in _with_nested(found)}


def _not_forbidding(models: dict[str, type[BaseModel]]) -> list[str]:
    """Bodies not on extra="forbid" and not allow-listed. An explicit
    extra="ignore" or "allow" is no excuse: it needs an ALLOWED_IGNORE row."""
    return sorted(
        key for key, model in models.items()
        if model.model_config.get("extra") != "forbid" and key not in ALLOWED_IGNORE
    )


def test_the_router_set_is_the_ownership_map():
    """Prove the walk sees what it should: an empty set would pass the guard."""
    mods = _owned_router_modules()
    for expected in ("customers", "leads", "portal", "surveys", "winback", "quote_requests"):
        assert f"gdx_dispatch.routers.{expected}" in mods
    assert "gdx_dispatch.core.locations" in mods
    assert mods["gdx_dispatch.routers.portal"] == ["router", "staff_router"]


def test_the_walk_finds_body_models(body_models):
    """Known bodies, one imported from a module and one nested, so the guard
    below cannot go quiet by finding nothing."""
    for key in (
        "gdx_dispatch.routers.customers:CustomerCreateIn",
        "gdx_dispatch.routers.surveys:PublicSurveyResponseIn",
        "gdx_dispatch.routers.portal:PortalInviteIn",
        "gdx_dispatch.modules.quote_requests.service:QuoteRequestIn",
        "gdx_dispatch.modules.quote_requests.service:DoorIn",
        "gdx_dispatch.core.locations:LocationCreate",
    ):
        assert key in body_models, key
    assert len(body_models) >= 50


def test_every_request_body_forbids_extra(body_models):
    """A body schema not on extra="forbid" drops (or keeps) unknown keys silently.

    Set ``model_config = ConfigDict(extra="forbid")`` after checking every
    caller sends only declared keys; or, if a caller legitimately sends more,
    add the schema to ALLOWED_IGNORE with the reason.
    """
    missing = _not_forbidding(body_models)
    assert not missing, "request bodies on the extra=ignore default:\n  " + "\n  ".join(missing)


def test_allow_list_names_real_body_models(body_models):
    """An allow-list row for a renamed or deleted schema hides nothing; delete it."""
    stale = sorted(set(ALLOWED_IGNORE) - set(body_models))
    assert not stale, stale


def test_forbid_refuses_an_unknown_key_on_every_forbidding_body(body_models):
    """Run the validator FastAPI runs, once per schema: the declaration is not
    the behaviour, so check the behaviour."""
    checked = 0
    for key, model in body_models.items():
        if model.model_config.get("extra") != "forbid":
            continue
        with pytest.raises(ValidationError) as exc:
            model.model_validate({"__not_a_field__": 1})
        types_ = {err["type"] for err in exc.value.errors()}
        assert "extra_forbidden" in types_, key
        checked += 1
    # 43 set by GDXA-1697, 2 by GDXA-1676 (survey templates), 4 already forbade.
    assert checked >= 49


class _Loose(BaseModel):
    name: str


class _ExplicitIgnore(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str


def test_the_guard_can_go_red():
    """A route body with no `extra`, or an explicit extra="ignore", is found
    by the walk and reported by the same check the guard runs."""
    from fastapi import FastAPI

    app = FastAPI()

    @app.post("/x")
    def _x(payload: _Loose) -> None:  # pragma: no cover - never called
        return None

    @app.post("/y")
    def _y(payload: _ExplicitIgnore) -> None:  # pragma: no cover - never called
        return None

    found: list[type[BaseModel]] = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            found.extend(_body_models(route.dependant))
    models = {_key(m): m for m in _with_nested(found)}
    assert set(models.values()) == {_Loose, _ExplicitIgnore}
    assert _not_forbidding(models) == sorted(models)


# --- the customer-facing surface, through the real HTTP stack ---------------


def test_public_survey_submit_refuses_an_unknown_key_and_keeps_the_token():
    """The one unauthenticated body in this set. A 422 must not burn the
    customer's link: the exact body survey_form.html posts still lands after."""
    from gdx_dispatch.tests.test_surveys import _make_client

    client = _make_client()
    try:
        tpl = client.post(
            "/api/surveys/templates", json={"name": "NPS", "kind": "nps", "question": "q"}
        ).json()
        token = client.post("/api/surveys/send", json={"template_id": tpl["id"]}).json()["token"]

        bad = client.post(f"/api/surveys/public/{token}", json={"score": 9, "rating": 9})
        assert bad.status_code == 422, bad.text
        assert any(e["type"] == "extra_forbidden" for e in bad.json()["detail"])

        # survey_form.html: JSON.stringify({score: selected, comment: ... || null})
        ok = client.post(f"/api/surveys/public/{token}", json={"score": 9, "comment": None})
        assert ok.status_code == 201, ok.text
    finally:
        client.app.dependency_overrides.clear()
        client._engine.dispose()  # type: ignore[attr-defined]
