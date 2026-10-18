"""GDXA-579: the onboarding wizard's technician step must send what the API accepts.

Step 2 of ``OnboardingView.vue`` used to POST ``{name, email, phone}`` to
``/api/technicians``. ``create_technician`` answers 400 "user_id is required"
to any payload without one (pinned by
``test_technicians.py::test_create_technician_requires_user_id``), so every
technician entered in the wizard stalled it on that step. The wizard now
creates the technician's login with ``POST /api/users`` and posts the
technician with the returned id.

This file is the backend half of that contract. It reads the object literals
the view really posts, so a key renamed on either side turns it red:

* every key the view sends to each endpoint is a field that endpoint's model
  declares (pydantic drops unknown keys silently, which is how this class of
  bug hides), and every required field is sent;
* the wizard's technician payload, with a real user's id, gets a 201 from the
  real handler, not a mock.

The behavioural half (call order, the id handed across, no second user on a
retry) is ``frontend/src/views/__tests__/OnboardingTechnicianStep.spec.js``.

Falsifier: drop ``user_id`` from the technician POST in the view and
``test_wizard_technician_payload_carries_user_id`` goes red; rename a key to
one ``TechnicianCreate`` does not declare and
``test_wizard_payload_keys_are_declared_fields`` goes red.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models on TenantBase.metadata)
from gdx_dispatch.models.tenant_models import Technician, User
from gdx_dispatch.routers.technicians import TechnicianCreate, create_technician
from gdx_dispatch.routers.users import UserCreateIn

VIEW = Path(__file__).resolve().parents[1] / "frontend" / "src" / "views" / "OnboardingView.vue"


def _posted_keys(url: str) -> list[str]:
    """Keys of the object literal passed to ``api.post('<url>', {...})``."""
    src = VIEW.read_text(encoding="utf-8")
    m = re.search(r"api\.post\(\s*'" + re.escape(url) + r"'\s*,\s*\{", src)
    assert m, f"OnboardingView.vue no longer posts an object literal to {url}"
    depth, i = 1, m.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
    body = src[m.end(): i - 1]
    keys = []
    for part in body.split(","):
        part = part.strip()
        if part:
            keys.append(re.match(r"([A-Za-z_]\w*)", part).group(1))
    return keys


def test_wizard_technician_payload_carries_user_id():
    assert "user_id" in _posted_keys("/api/technicians")


@pytest.mark.parametrize(
    ("url", "model"),
    [("/api/technicians", TechnicianCreate), ("/api/users", UserCreateIn)],
)
def test_wizard_payload_keys_are_declared_fields(url, model):
    keys = set(_posted_keys(url))
    assert keys <= set(model.model_fields), f"{url}: undeclared keys {keys - set(model.model_fields)}"
    required = {name for name, f in model.model_fields.items() if f.is_required()}
    assert required <= keys, f"{url}: required fields not sent {required - keys}"


def test_wizard_user_payload_validates():
    """The literal role the wizard sends passes UserCreateIn's role pattern."""
    src = VIEW.read_text(encoding="utf-8")
    role = re.search(r"role:\s*'(\w+)'", src).group(1)
    u = UserCreateIn(name="Pat Tech", email="pat@example.com", password="longenough1", role=role)
    assert u.role == "technician"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()
    engine.dispose()


def test_wizard_technician_payload_is_accepted(db):
    """A real create_technician call with exactly the keys the wizard sends."""
    req = Request({"type": "http", "headers": []})
    req.state.tenant = {"id": "tenant-a"}
    user_id = str(uuid.uuid4())  # what POST /api/users returns as `id`
    # GDXA-1583: create_technician refuses a user_id with no user behind it.
    db.add(User(id=uuid.UUID(user_id), company_id="tenant-a", email="pat@example.com"))
    db.commit()
    sample = {"user_id": user_id, "name": "Pat Tech", "email": "pat@example.com", "phone": "555-0100"}
    payload = {k: sample[k] for k in _posted_keys("/api/technicians")}

    resp = create_technician(TechnicianCreate(**payload), req, {"user_id": "owner-1"}, db)

    assert resp.status_code == 201, resp.body
    body = json.loads(resp.body.decode())
    assert body["user_id"] == user_id
    row = db.execute(select(Technician).where(Technician.id == body["id"])).scalar_one()
    assert (row.user_id, row.name, row.email, row.phone) == (user_id, "Pat Tech", "pat@example.com", "555-0100")
