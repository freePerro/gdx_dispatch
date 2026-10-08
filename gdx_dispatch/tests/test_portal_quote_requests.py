"""Portal quote requests.

A signed-in portal customer asks for new doors under a job name; the request
opens a Lead already tied to that customer, the office is alerted, and the
customer's status follows the lead's estimates — not its stage, which Start
Estimate and sending a quote never write.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.models  # noqa: F401  (registers every model on TenantBase)
import gdx_dispatch.routers.quote_requests as qr_router
from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Customer, Lead, Notification, User
from gdx_dispatch.modules.proposals.models import Estimate
from gdx_dispatch.modules.quote_requests import service
from gdx_dispatch.modules.quote_requests.models import QuoteRequest, QuoteRequestPhoto
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.leads import router as leads_router
from gdx_dispatch.routers.portal import PortalPrincipal, get_current_portal_customer
from gdx_dispatch.routers.quote_requests import portal_router, router

TENANT = "tenant-test"
_STAFF_UID = "00000000-0000-0000-0000-0000000000a1"
_CUST_A = UUID("00000000-0000-0000-0000-0000000000c1")
_CUST_B = UUID("00000000-0000-0000-0000-0000000000c2")
_USER_A = UUID("00000000-0000-0000-0000-0000000000d1")
_USER_B = UUID("00000000-0000-0000-0000-0000000000d2")

# The real 4x4 PNG from test_door_listings.py (Pillow-generated, decodable).
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000004000000040802000000269309"
    "290000001449444154789c633c5161c300034c0c48003707004bc801849e5e42"
    "fa0000000049454e44ae426082"
)

DOOR = {"width_ft": 16, "height_ft": 7, "style": "carriage_house", "opener": "yes"}


class _Ctx:
    def __init__(self, tmp_path, staff_role: str = "admin"):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        TenantBase.metadata.create_all(self.engine, checkfirst=True)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        s = self.Session()
        s.execute(text(
            "CREATE TABLE IF NOT EXISTS company_module_grants (id TEXT PRIMARY KEY, "
            "company_id TEXT, module_key TEXT, granted_at TEXT, created_at TEXT, expires_at TEXT)"
        ))
        for key in ("customer_portal", "customers"):
            s.execute(
                text(
                    "INSERT INTO company_module_grants (id, company_id, module_key, granted_at, created_at) "
                    "VALUES (:id, :tid, :k, datetime('now'), datetime('now'))"
                ),
                {"id": f"cmg-{key}", "tid": TENANT, "k": key},
            )
        s.add(User(id=UUID(_STAFF_UID), email="staff@example.com", password_hash="x",
                   role=staff_role, active=True, company_id=TENANT))
        s.add(Customer(id=_CUST_A, name="Pat Example", email="pat@example.com",
                       phone="555-0101", address="1 Main St", company_id=TENANT))
        s.add(Customer(id=_CUST_B, name="Other Person", company_id=TENANT))
        s.commit()
        s.close()
        self.principal = PortalPrincipal(user_id=_USER_A, customer_id=_CUST_A, role="customer")

        def _db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app = FastAPI()

        @app.middleware("http")
        async def inject_tenant(request, call_next):
            request.state.tenant = {"id": TENANT}
            return await call_next(request)

        app.include_router(portal_router)
        app.include_router(router)
        app.include_router(leads_router)
        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_current_portal_customer] = lambda: self.principal
        app.dependency_overrides[get_current_user] = lambda: {
            "user_id": _STAFF_UID, "sub": _STAFF_UID, "role": staff_role, "tenant_id": TENANT,
        }
        self.client = TestClient(app, raise_server_exceptions=True)

    def as_customer_b(self):
        self.principal = PortalPrincipal(user_id=_USER_B, customer_id=_CUST_B, role="customer")

    def submit(self, **over) -> dict:
        body = {"job_name": "Lake cabin", "doors": [DOOR]}
        body.update(over)
        r = self.client.post("/portal/quote-requests", json=body)
        assert r.status_code == 201, r.text
        return r.json()


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    c = _Ctx(tmp_path)
    yield c
    c.engine.dispose()


def test_submit_records_the_doors_and_opens_a_lead_for_this_customer(ctx):
    out = ctx.submit(doors=[DOOR, {"width_ft": 9, "height_ft": 7, "quantity": 2}], site_address="  ")
    assert out["status"] == "Received"
    assert [d["size_label"] for d in out["doors"]] == ["16' 0\" x 7' 0\"", "9' 0\" x 7' 0\""]
    assert out["doors"][0]["style"] == "carriage_house"
    assert out["doors"][1]["material"] == "not_sure"  # unanswered → not_sure, never invented

    s = ctx.Session()
    req = s.get(QuoteRequest, UUID(out["id"]))
    assert req.customer_id == _CUST_A and req.submitted_by_user_id == _USER_A
    assert req.site_address is None  # blank stays blank
    lead = s.get(Lead, req.lead_id)
    assert lead.converted_customer_id == _CUST_A
    assert lead.stage == "new" and lead.source == "Customer portal"
    assert lead.origin_ref == f"quote_request:{req.id}"
    assert lead.company_id == TENANT
    assert lead.address == "1 Main St"  # the customer's, when no site address was given
    assert lead.created_by == f"portal:{_USER_A}"
    # The request is the only copy of the doors; the lead names the job and count.
    assert "Lake cabin" in lead.notes and "3 doors" in lead.notes
    assert "carriage" not in lead.notes
    s.close()


def test_submit_is_audited_and_alerts_the_office(ctx):
    out = ctx.submit()
    s = ctx.Session()
    actions = {a.action: a for a in s.execute(select(AuditLog)).scalars()}
    assert actions["quote_request_submitted"].entity_id == out["id"]
    assert actions["quote_request_submitted"].user_id == f"portal:{_USER_A}"
    assert "lead_created" in actions
    note = s.execute(select(Notification)).scalars().one()
    assert note.category == "lead" and "Pat Example" in note.title and "Lake cabin" in note.message
    s.close()


@pytest.mark.parametrize("body", [
    {"job_name": "", "doors": [DOOR]},
    {"job_name": "x", "doors": []},
    {"job_name": "x", "doors": [DOOR] * 11},
    {"job_name": "x", "doors": [{"height_ft": 7}]},  # size is required
    {"job_name": "x", "doors": [{**DOOR, "width_ft": 2}]},
    {"job_name": "x", "doors": [{**DOOR, "style": "gothic"}]},
])
def test_submit_refuses_a_malformed_request(ctx, body):
    assert ctx.client.post("/portal/quote-requests", json=body).status_code == 422


def test_a_customer_sees_only_their_own_requests(ctx):
    mine = ctx.submit()
    ctx.as_customer_b()
    assert ctx.client.get("/portal/quote-requests").json() == []
    r = ctx.client.post(
        f"/portal/quote-requests/{mine['id']}/photos",
        data={"door_index": "0"}, files={"file": ("d.png", _PNG, "image/png")},
    )
    assert r.status_code == 404


def test_a_stored_photo_carries_no_gps(ctx):
    """A phone stamps GPS on a photo taken in someone's driveway; what staff
    are served must not carry it. Counterfactual: store the upload bytes as
    sent and the GPS IFD comes back."""
    import io

    from PIL import Image

    exif = Image.Exif()
    exif[0x0110] = "Pixel 8"  # Model
    exif[0x8825] = {1: "N", 2: (44.0, 58.0, 30.0), 3: "W", 4: (93.0, 15.0, 50.0)}  # GPSInfo
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (180, 120, 60)).save(buf, "JPEG", exif=exif)
    sent = buf.getvalue()
    assert Image.open(io.BytesIO(sent)).getexif().get_ifd(0x8825), "fixture must carry GPS"

    out = ctx.submit()
    r = ctx.client.post(
        f"/portal/quote-requests/{out['id']}/photos",
        data={"door_index": "0"}, files={"file": ("door.jpg", sent, "image/jpeg")},
    )
    assert r.status_code == 201, r.text
    served = ctx.client.get(f"/api/quote-requests/{out['id']}/photos/{r.json()['id']}")
    assert served.status_code == 200
    stored = Image.open(io.BytesIO(served.content)).getexif()
    assert not stored.get_ifd(0x8825)
    assert 0x0110 not in stored


def test_photos_attach_to_a_door_and_are_capped(ctx):
    out = ctx.submit()
    url = f"/portal/quote-requests/{out['id']}/photos"
    for _ in range(service.MAX_PHOTOS_PER_DOOR):
        r = ctx.client.post(url, data={"door_index": "0"}, files={"file": ("d.png", _PNG, "image/png")})
        assert r.status_code == 201, r.text
    over = ctx.client.post(url, data={"door_index": "0"}, files={"file": ("d.png", _PNG, "image/png")})
    assert over.status_code == 422
    no_door = ctx.client.post(url, data={"door_index": "1"}, files={"file": ("d.png", _PNG, "image/png")})
    assert no_door.status_code == 422
    fresh = f"/portal/quote-requests/{ctx.submit()['id']}/photos"
    bad_type = ctx.client.post(fresh, data={"door_index": "0"}, files={"file": ("d.gif", b"GIF89a", "image/gif")})
    assert bad_type.status_code == 415

    listed = next(r for r in ctx.client.get("/portal/quote-requests").json() if r["id"] == out["id"])
    assert len(listed["doors"][0]["photo_ids"]) == service.MAX_PHOTOS_PER_DOOR
    s = ctx.Session()
    assert s.execute(
        select(AuditLog).where(AuditLog.action == "quote_request_photo_added")
    ).scalars().all().__len__() == service.MAX_PHOTOS_PER_DOOR
    s.close()


def test_staff_read_the_request_from_its_lead_and_fetch_a_photo(ctx):
    out = ctx.submit()
    photo = ctx.client.post(
        f"/portal/quote-requests/{out['id']}/photos",
        data={"door_index": "0"}, files={"file": ("d.png", _PNG, "image/png")},
    ).json()
    s = ctx.Session()
    lead_id = s.get(QuoteRequest, UUID(out["id"])).lead_id
    s.close()

    got = ctx.client.get(f"/api/quote-requests/by-lead/{lead_id}").json()
    assert got["id"] == out["id"] and got["job_name"] == "Lake cabin"
    assert got["doors"][0]["photo_ids"] == [photo["id"]]
    img = ctx.client.get(f"/api/quote-requests/{out['id']}/photos/{photo['id']}")
    assert img.status_code == 200 and img.headers["content-type"].startswith("image/")
    # A photo id under the wrong request is not found.
    assert ctx.client.get(f"/api/quote-requests/{uuid4()}/photos/{photo['id']}").status_code == 404
    assert ctx.client.get(f"/api/quote-requests/by-lead/{uuid4()}").json() is None


def test_staff_without_leads_read_are_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    c = _Ctx(tmp_path, staff_role="technician")
    try:
        assert c.client.get(f"/api/quote-requests/by-lead/{uuid4()}").status_code == 403
    finally:
        c.engine.dispose()


def _add_estimate(ctx, lead_id, status):
    s = ctx.Session()
    s.add(Estimate(customer_id=_CUST_A, lead_id=lead_id, status=status, estimate_number=f"EST-{uuid4().hex[:6]}", company_id=TENANT, public_token=uuid4().hex,
                   created_at=datetime.now(UTC)))
    s.commit()
    s.close()


def test_status_follows_the_estimates_not_the_lead_stage(ctx):
    out = ctx.submit()
    s = ctx.Session()
    lead_id = s.get(QuoteRequest, UUID(out["id"])).lead_id
    s.close()

    def status():
        return ctx.client.get("/portal/quote-requests").json()[0]["status"]

    assert status() == "Received"
    _add_estimate(ctx, lead_id, "draft")  # Start Estimate: the stage stays "new"
    assert status() == "Being priced"
    _add_estimate(ctx, lead_id, "sent")
    assert status() == "Quote ready"


def test_a_lead_closed_with_no_estimate_reads_closed(ctx):
    out = ctx.submit()
    s = ctx.Session()
    lead = s.get(Lead, s.get(QuoteRequest, UUID(out["id"])).lead_id)
    lead.stage = "lost"
    s.commit()
    s.close()
    assert ctx.client.get("/portal/quote-requests").json()[0]["status"] == "Closed"


def test_customer_status_mapping():
    assert service.customer_status(None, None) == "Received"
    assert service.customer_status(None, {"stage": "sold"}) == "Accepted"
    assert service.customer_status(None, {"stage": "scheduled"}) == "Accepted"
    assert service.customer_status(None, {"stage": "declined"}) == "Declined"
    assert service.customer_status(None, {"stage": "expired"}) == "Expired"
    assert service.customer_status(SimpleNamespace(stage="won"), {"stage": "estimate_started"}) == "Accepted"
    assert service.customer_status(SimpleNamespace(stage="quoted"), {"stage": "estimate_started"}) == "Quote ready"
    assert service.customer_status(SimpleNamespace(stage="quoted"), {"stage": "declined"}) == "Declined"
    assert service.customer_status(SimpleNamespace(stage="contacted"), None) == "Received"
    assert service.customer_status(SimpleNamespace(stage="lost"), {"stage": "estimate_started"}) == "Closed"
    # Accepting marks the lead won on every path, so a dead job's lead is won.
    won = SimpleNamespace(stage="won")
    assert service.customer_status(won, {"stage": "cancelled", "type": "lost"}) == "Closed"
    assert service.customer_status(won, {"stage": "written_off", "type": "lost"}) == "Closed"
    assert service.customer_status(won, {"stage": "completed", "type": "won"}) == "Accepted"
    assert service.customer_status(won, {"stage": "sold", "type": "open"}) == "Accepted"


@pytest.mark.parametrize("close", ["lost_after_start", "deleted"])
def test_a_lead_staff_closed_locks_the_request(ctx, close):
    out = ctx.submit()
    lead_id = _lead_id(ctx, out["id"])
    if close == "lost_after_start":
        _add_estimate(ctx, lead_id, "draft")
    s = ctx.Session()
    lead = s.get(Lead, lead_id)
    if close == "deleted":
        lead.deleted_at = datetime.now(UTC)
    else:
        lead.stage = "lost"
    s.commit()
    s.close()
    listed = ctx.client.get("/portal/quote-requests").json()[0]
    assert listed["status"] == "Closed"
    assert listed["can_edit"] is False and listed["can_withdraw"] is False
    assert ctx.client.patch(
        f"/portal/quote-requests/{out['id']}", json={"job_name": "x", "doors": [DOOR]}
    ).status_code == 409


def test_photo_path_is_fenced_to_the_upload_root(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    p = service.photo_path(TENANT, uuid4(), "../../etc/passwd")
    assert str(p).startswith(str(tmp_path)) and p.name == "passwd"


def test_photo_rows_reference_their_request(ctx):
    out = ctx.submit()
    ctx.client.post(
        f"/portal/quote-requests/{out['id']}/photos",
        data={"door_index": "0"}, files={"file": ("d.png", _PNG, "image/png")},
    )
    s = ctx.Session()
    row = s.execute(select(QuoteRequestPhoto)).scalars().one()
    assert str(row.quote_request_id) == out["id"] and row.door_index == 0
    s.close()


# ── Start Estimate, edit, withdraw ───────────────────────────────────────────


def _lead_id(ctx, request_id) -> UUID:
    s = ctx.Session()
    lead_id = s.get(QuoteRequest, UUID(request_id)).lead_id
    s.close()
    return lead_id


def _photo(ctx, request_id, door_index) -> str:
    r = ctx.client.post(
        f"/portal/quote-requests/{request_id}/photos",
        data={"door_index": str(door_index)}, files={"file": ("d.png", _PNG, "image/png")},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_start_estimate_names_the_estimate_after_the_requests_job(ctx):
    out = ctx.submit(job_name="Lake cabin, both doors")
    r = ctx.client.post(f"/api/leads/{_lead_id(ctx, out['id'])}/start-estimate")
    assert r.status_code == 200, r.text
    assert r.json()["estimate"]["label"] == "Lake cabin, both doors"
    s = ctx.Session()
    assert s.get(Estimate, UUID(r.json()["estimate"]["id"])).label == "Lake cabin, both doors"
    s.close()


def test_a_customer_edits_a_request_and_the_office_is_told_when(ctx):
    two = {"width_ft": 9, "height_ft": 7}
    out = ctx.submit(doors=[DOOR, two])
    kept = _photo(ctx, out["id"], 1)
    dropped = _photo(ctx, out["id"], 0)

    # Remove the first door, keep the second (now first), add a new one.
    body = {
        "job_name": "Lake cabin (revised)",
        "doors": [{**two, "quantity": 2, "source_index": 1}, {"width_ft": 8, "height_ft": 7}],
    }
    r = ctx.client.patch(f"/portal/quote-requests/{out['id']}", json=body)
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["job_name"] == "Lake cabin (revised)" and got["edited_at"]
    assert got["doors"][0]["quantity"] == 2 and "source_index" not in got["doors"][0]
    assert got["doors"][0]["photo_ids"] == [kept]  # the photo followed its door
    assert got["doors"][1]["photo_ids"] == []

    s = ctx.Session()
    gone = s.get(QuoteRequestPhoto, UUID(dropped))
    assert gone.deleted_at is not None  # soft-deleted with its door, not unlinked
    audit = s.execute(select(AuditLog).where(AuditLog.action == "quote_request_edited")).scalars().one()
    assert audit.user_id == f"portal:{_USER_A}"
    changes = audit.details["changes"]
    assert changes["job_name"] == {"from": "Lake cabin", "to": "Lake cabin (revised)"}
    assert len(changes["doors"]["from"]) == 2 and len(changes["doors"]["to"]) == 2
    assert changes["removed_photo_ids"] == [dropped]
    lead = s.get(Lead, _lead_id(ctx, out["id"]))
    assert "Lake cabin (revised)" in lead.notes and "3 doors" in lead.notes
    note = s.execute(
        select(Notification).where(Notification.title.like("Quote request edited%"))
    ).scalars().one()
    assert "Pat Example" in note.title
    assert "job name" in note.message and "doors" in note.message and "UTC" not in note.message
    s.close()
    # The staff photo route no longer serves the removed photo.
    assert ctx.client.get(f"/api/quote-requests/{out['id']}/photos/{dropped}").status_code == 404


def test_editing_leaves_notes_staff_have_rewritten(ctx):
    out = ctx.submit()
    s = ctx.Session()
    lead = s.get(Lead, _lead_id(ctx, out["id"]))
    lead.notes = "Called back, wants a weekday visit."
    s.commit()
    s.close()
    assert ctx.client.patch(
        f"/portal/quote-requests/{out['id']}", json={"job_name": "Renamed", "doors": [DOOR]}
    ).status_code == 200
    s = ctx.Session()
    assert s.get(Lead, _lead_id(ctx, out["id"])).notes == "Called back, wants a weekday visit."
    s.close()


@pytest.mark.parametrize("doors", [
    [{**DOOR, "source_index": 0}, {**DOOR, "source_index": 0}],  # one door twice
    [{**DOOR, "source_index": 5}],  # no such door
])
def test_an_edit_that_names_doors_wrongly_is_refused(ctx, doors):
    out = ctx.submit()
    r = ctx.client.patch(f"/portal/quote-requests/{out['id']}", json={"job_name": "x", "doors": doors})
    assert r.status_code == 422


def test_an_unchanged_edit_is_not_stamped_or_announced(ctx):
    out = ctx.submit()
    r = ctx.client.patch(
        f"/portal/quote-requests/{out['id']}",
        json={"job_name": "Lake cabin", "doors": [{**DOOR, "source_index": 0}]},
    )
    assert r.status_code == 200 and r.json()["edited_at"] is None
    s = ctx.Session()
    assert not s.execute(select(AuditLog).where(AuditLog.action == "quote_request_edited")).scalars().all()
    assert len(s.execute(select(Notification)).scalars().all()) == 1  # the submit alert only
    s.close()


def test_a_request_cannot_be_edited_once_a_quote_is_ready(ctx):
    out = ctx.submit()
    _add_estimate(ctx, _lead_id(ctx, out["id"]), "sent")
    listed = ctx.client.get("/portal/quote-requests").json()[0]
    # The quote itself is what the customer accepts or declines now; a
    # withdraw would leave it live and acceptable.
    assert listed["can_edit"] is False and listed["can_withdraw"] is False
    r = ctx.client.patch(f"/portal/quote-requests/{out['id']}", json={"job_name": "x", "doors": [DOOR]})
    assert r.status_code == 409
    assert ctx.client.post(f"/portal/quote-requests/{out['id']}/withdraw").status_code == 409


@pytest.mark.parametrize(("stage", "status"), [("won", "Accepted"), ("quoted", "Quote ready")])
def test_a_lead_staff_moved_by_hand_locks_the_request(ctx, stage, status):
    """Sold or quoted over the phone: no estimate says so, the stage does."""
    out = ctx.submit()
    s = ctx.Session()
    s.get(Lead, _lead_id(ctx, out["id"])).stage = stage
    s.commit()
    s.close()
    listed = ctx.client.get("/portal/quote-requests").json()[0]
    assert listed["status"] == status
    assert listed["can_edit"] is False and listed["can_withdraw"] is False
    assert ctx.client.patch(
        f"/portal/quote-requests/{out['id']}", json={"job_name": "x", "doors": [DOOR]}
    ).status_code == 409
    assert ctx.client.post(f"/portal/quote-requests/{out['id']}/withdraw").status_code == 409


def test_the_gate_refuses_when_the_estimates_cannot_be_read(ctx, monkeypatch):
    """The progress helper returns {} on a read error; with estimates on the
    lead, that must not read as "Received" and let a change through."""
    out = ctx.submit()
    _add_estimate(ctx, _lead_id(ctx, out["id"]), "accepted")
    monkeypatch.setattr(qr_router, "_progress_for_leads", lambda db, leads: {})
    assert ctx.client.patch(
        f"/portal/quote-requests/{out['id']}", json={"job_name": "x", "doors": [DOOR]}
    ).status_code == 409
    assert ctx.client.post(f"/portal/quote-requests/{out['id']}/withdraw").status_code == 409


def test_another_customer_can_neither_edit_nor_withdraw(ctx):
    out = ctx.submit()
    ctx.as_customer_b()
    assert ctx.client.patch(
        f"/portal/quote-requests/{out['id']}", json={"job_name": "x", "doors": [DOOR]}
    ).status_code == 404
    assert ctx.client.post(f"/portal/quote-requests/{out['id']}/withdraw").status_code == 404


def test_a_customer_withdraws_a_request_and_the_office_is_told_when(ctx):
    out = ctx.submit()
    r = ctx.client.post(f"/portal/quote-requests/{out['id']}/withdraw")
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["status"] == "Withdrawn" and got["withdrawn_at"]
    assert got["can_edit"] is False and got["can_withdraw"] is False
    assert ctx.client.get("/portal/quote-requests").json()[0]["status"] == "Withdrawn"

    s = ctx.Session()
    req = s.get(QuoteRequest, UUID(out["id"]))
    assert req.withdrawn_at is not None and req.deleted_at is None  # kept on record
    assert s.get(Lead, req.lead_id).stage == "lost"
    actions = {a.action: a for a in s.execute(select(AuditLog)).scalars()}
    assert actions["quote_request_withdrawn"].details["status_before"] == "Received"
    assert actions["lead_updated"].details["stage"] == {"from": "new", "to": "lost"}
    assert actions["lead_updated"].user_id == f"portal:{_USER_A}"
    note = s.execute(
        select(Notification).where(Notification.title.like("Quote request withdrawn%"))
    ).scalars().one()
    assert "Lake cabin" in note.message and "UTC" not in note.message and "lost" in note.message
    s.close()

    # Withdrawn is final from the portal: no second withdraw, no edit, no photos.
    assert ctx.client.post(f"/portal/quote-requests/{out['id']}/withdraw").status_code == 409
    assert ctx.client.patch(
        f"/portal/quote-requests/{out['id']}", json={"job_name": "x", "doors": [DOOR]}
    ).status_code == 409
    assert ctx.client.post(
        f"/portal/quote-requests/{out['id']}/photos",
        data={"door_index": "0"}, files={"file": ("d.png", _PNG, "image/png")},
    ).status_code == 409
    # Staff still read it, stamped.
    staff = ctx.client.get(f"/api/quote-requests/by-lead/{req.lead_id}").json()
    assert staff["status"] == "Withdrawn" and staff["withdrawn_at"]


def test_an_accepted_quote_cannot_be_withdrawn(ctx):
    out = ctx.submit()
    _add_estimate(ctx, _lead_id(ctx, out["id"]), "accepted")
    assert ctx.client.post(f"/portal/quote-requests/{out['id']}/withdraw").status_code == 409


def test_customer_status_reads_withdrawn_first():
    assert service.customer_status(None, {"stage": "quoted"}, withdrawn=True) == "Withdrawn"


def test_the_office_alert_tells_the_time_in_the_shops_zone(ctx):
    """The containers run on UTC; 03:30 UTC is 10:30 the evening before in a
    Minnesota shop, and the alert must say the time the office lived."""
    from gdx_dispatch.models.tenant_models import AppSettings
    from gdx_dispatch.routers.quote_requests import _stamp

    db = ctx.Session()
    db.add(AppSettings(timezone="America/Chicago"))
    db.commit()
    assert _stamp(db, datetime(2026, 10, 8, 3, 30, tzinfo=UTC)) == "Oct 7, 2026 10:30 PM"
    db.close()
