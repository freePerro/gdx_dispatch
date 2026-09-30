"""HubX order submission email → the job's doors go "ordered".

The QCD quote number is the only key: the same number sits on the estimate
line's captured door spec and, after conversion, in the door's parts-row notes.
The parts rows here are built by the REAL estimate-to-job copy
(``routers.estimates._copy_estimate_lines_to_job``), so a change to how that
copy writes ``Number=QCD…`` breaks these tests instead of the feature.

All names and numbers below are invented.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.models.tenant_models import Customer, HoldingArea, Job, JobPartNeeded
from gdx_dispatch.modules.outlook.models import OutlookAccount, OutlookMessage
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.modules.vendor_orders.hubx import (
    ACTOR,
    HubxItem,
    apply_hubx_item,
    body_to_text,
    door_order_status_for_jobs,
    is_hubx_submission,
    notes_qcd,
    parse_hubx_submission,
    process_hubx_orders,
)
from gdx_dispatch.modules.vendor_orders.models import HubxDoorOrder, HubxOrderEmail
from gdx_dispatch.routers.estimates import _copy_estimate_lines_to_job

TID = "tenant-test"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)

# The shape of a real two-door submission body (HTML, one field per cell).
TWO_DOOR_HTML = """
<html><body><table>
<tr><td>User Name:</td><td>Office User</td></tr>
<tr><td>Sub Dealer Name:</td><td>Example Doors</td></tr>
<tr><td>Need By Date:</td><td>01/01/0001</td></tr>
</table>
<h3>Item 1</h3>
<table>
<tr><td>Quantity:</td><td>1</td></tr>
<tr><td>Quote Number:</td><td>QCD1000001</td></tr>
<tr><td>Date Submitted:</td><td>9/28/2026 4:06:49 PM</td></tr>
<tr><td>Job/PO Name:</td><td>xperts Example Job</td></tr>
<tr><td>Model Number:</td><td>5283</td></tr>
<tr><td>Size:</td><td>18 ft 0 in X 8 ft 0 in</td></tr>
</table>
<h3>Item 2</h3>
<table>
<tr><td>Quantity:</td><td>2</td></tr>
<tr><td>Quote Number:</td><td>QCD1000002</td></tr>
<tr><td>Date Submitted:</td><td>9/28/2026 4:06:49 PM</td></tr>
<tr><td>Job/PO Name:</td><td>xperts Example Job</td></tr>
<tr><td>Model Number:</td><td>5283</td></tr>
<tr><td>Size:</td><td>9 ft 0 in X 8 ft 0 in</td></tr>
</table></body></html>
"""

# The stored 255-char preview: tab-separated, one line per field, cut short.
PREVIEW = (
    "User Name:\tOffice User\r\nSub Dealer Name:\tExample Doors\r\n"
    "Need By Date:\t01/01/0001\r\nItem 1\r\nQuantity:\t1\r\n"
    "Quote Number:\tQCD1000009\r\nDate Submitted:\t9/28/2026 10:30:28 PM\r\nJob/PO Name:\txpe"
)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _area(db, name):
    a = HoldingArea(id=str(uuid4()), company_id=TID, name=name, sort_order=0)
    db.add(a)
    db.flush()
    return a


@pytest.fixture()
def areas(db):
    out = {n: _area(db, n) for n in ("Order Doors", "Need to order", "Waiting on doors", "Ready to Schedule")}
    db.commit()
    return out


def _door_md(qcd):
    return {
        "Number": qcd, "Cart Name": "example", "Model": "Example Collection, 5283 Model",
        "Spring": "Torsion", "Track": "2in", "Price": "1000.00",
    }


def _sold_job(db, *, qcds, area=None, stage="scheduled", status="accepted", labor=True):
    """An accepted estimate with one captured door per QCD, converted to a job
    through the real copy helper."""
    cust = Customer(name=f"Customer {uuid4().hex[:6]}", company_id=TID)
    db.add(cust)
    db.flush()
    job = Job(customer_id=cust.id, company_id=TID, title="Door install",
              lifecycle_stage=stage, holding_area_id=str(area.id) if area else None)
    db.add(job)
    db.flush()
    est = Estimate(estimate_number=f"EST-{uuid4().hex[:8]}", customer_id=cust.id,
                   status=status, job_id=job.id, company_id=TID, public_token=uuid4().hex)
    db.add(est)
    db.flush()
    for i, qcd in enumerate(qcds, start=1):
        db.add(EstimateLine(estimate_id=est.id, description=f"Door {qcd}", category="Doors",
                            quantity=1, unit_price=1500, line_total=1500, sort_order=i,
                            line_metadata=_door_md(qcd), company_id=TID))
    if labor:
        db.add(EstimateLine(estimate_id=est.id, description="Install", category="Labor",
                            quantity=1, unit_price=500, line_total=500, sort_order=99,
                            company_id=TID))
    db.flush()
    _copy_estimate_lines_to_job(est, job, db)
    db.commit()
    return job, est


def _parts(db, job):
    return {
        (notes_qcd(p.notes) or p.part_name): p.status
        for p in db.execute(select(JobPartNeeded).where(JobPartNeeded.job_id == str(job.id))).scalars()
    }


def _apply(db, qcd, msg_id="msg-1"):
    rec = apply_hubx_item(db, HubxItem(qcd=qcd), graph_message_id=msg_id, received_at=NOW)
    db.commit()
    return rec


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def test_parses_every_item_from_the_html_body():
    items = parse_hubx_submission(body_to_text(TWO_DOOR_HTML, "html"))
    assert [i.qcd for i in items] == ["QCD1000001", "QCD1000002"]
    assert items[1].quantity == 2
    assert items[0].job_po_name == "xperts Example Job"
    assert items[0].model_number == "5283"
    assert items[1].size == "9 ft 0 in X 8 ft 0 in"


def test_parses_the_one_line_preview_shape():
    items = parse_hubx_submission(body_to_text(PREVIEW, "text"))
    assert [i.qcd for i in items] == ["QCD1000009"]


def test_ignores_items_without_a_qcd_and_repeated_qcds():
    text = "Item 1\nQuote Number:\tnot-a-quote\nItem 2\nQuote Number:\tqcd1000003\nItem 3\nQuote Number:\tQCD1000003\n"
    assert [i.qcd for i in parse_hubx_submission(text)] == ["QCD1000003"]


def test_only_the_order_submission_from_hubx_is_a_candidate():
    assert is_hubx_submission("HubX@ChiOHD.com", "Order submission from SubDealer X1")
    assert not is_hubx_submission("hubx@chiohd.com", "Quote saved")
    assert not is_hubx_submission("someone@example.com", "Order submission from SubDealer X1")


def test_notes_qcd_respects_the_number_boundary():
    assert notes_qcd("Doors • $1.00 ea • Number=QCD3931957; Model=x") == "QCD3931957"
    assert notes_qcd("Number=QCD39319570; Model=x") == "QCD39319570"
    assert notes_qcd("Labor • $500.00 ea") is None


# --------------------------------------------------------------------------- #
# applying
# --------------------------------------------------------------------------- #
def test_all_doors_ordered_marks_parts_and_moves_the_job(db, areas):
    job, est = _sold_job(db, qcds=["QCD1000001", "QCD1000002"], area=areas["Order Doors"])

    r1 = _apply(db, "QCD1000001")
    assert r1.outcome == "applied" and r1.parts_marked == 1
    assert r1.moved_to_area is None, "one door still needed — the job stays put"
    db.refresh(job)
    assert job.holding_area_id == str(areas["Order Doors"].id)

    r2 = _apply(db, "QCD1000002")
    assert r2.parts_marked == 1
    assert (r2.moved_from_area, r2.moved_to_area) == ("Order Doors", "Waiting on doors")
    db.refresh(job)
    assert job.holding_area_id == str(areas["Waiting on doors"].id)
    assert r2.matched_job_id == job.id and r2.matched_estimate_id == est.id

    parts = _parts(db, job)
    assert parts["QCD1000001"] == "ordered" and parts["QCD1000002"] == "ordered"
    assert parts["Install"] == "needed", "labor rows are not doors"


def test_every_change_is_audited_with_its_source(db, areas):
    job, _ = _sold_job(db, qcds=["QCD1000001"], area=areas["Need to order"])
    _apply(db, "QCD1000001", msg_id="msg-audit")
    rows = db.execute(select(AuditLog).where(AuditLog.user_id == ACTOR)).scalars().all()
    by_type = {r.entity_type: r for r in rows}
    assert set(by_type) == {"part_needed", "job"}
    assert by_type["part_needed"].details["status"] == {"from": "needed", "to": "ordered"}
    assert by_type["job"].details["holding_area"] == {"from": "Need to order", "to": "Waiting on doors"}
    for r in rows:
        assert r.details["qcd"] == "QCD1000001"
        assert r.details["graph_message_id"] == "msg-audit"
        assert r.details["via"] == "hubx_order_email"


def test_a_job_in_another_lane_is_not_moved(db, areas):
    job, _ = _sold_job(db, qcds=["QCD1000001"], area=areas["Ready to Schedule"])
    rec = _apply(db, "QCD1000001")
    assert rec.parts_marked == 1 and rec.moved_to_area is None
    db.refresh(job)
    assert job.holding_area_id == str(areas["Ready to Schedule"].id)


def test_no_destination_lane_means_no_move(db):
    order = _area(db, "Order Doors")
    job, _ = _sold_job(db, qcds=["QCD1000001"], area=order)
    rec = _apply(db, "QCD1000001")
    assert rec.parts_marked == 1 and rec.moved_to_area is None
    db.refresh(job)
    assert job.holding_area_id == str(order.id)


def test_closed_jobs_are_never_rewritten(db, areas):
    job, _ = _sold_job(db, qcds=["QCD1000001"], area=areas["Order Doors"], stage="completed")
    rec = _apply(db, "QCD1000001")
    assert rec.outcome == "job_closed" and rec.parts_marked == 0
    assert _parts(db, job)["QCD1000001"] == "needed"


def test_an_unaccepted_estimate_is_not_a_match(db, areas):
    job, _ = _sold_job(db, qcds=["QCD1000001"], area=areas["Order Doors"], status="sent")
    rec = _apply(db, "QCD1000001")
    assert rec.outcome == "no_estimate"
    assert _parts(db, job)["QCD1000001"] == "needed"


def test_a_qcd_on_two_jobs_changes_nothing(db, areas):
    job_a, _ = _sold_job(db, qcds=["QCD1000001"], area=areas["Order Doors"])
    job_b, _ = _sold_job(db, qcds=["QCD1000001"], area=areas["Order Doors"])
    rec = _apply(db, "QCD1000001")
    assert rec.outcome == "ambiguous"
    assert _parts(db, job_a)["QCD1000001"] == "needed"
    assert _parts(db, job_b)["QCD1000001"] == "needed"


def test_only_needed_rows_flip(db, areas):
    job, _ = _sold_job(db, qcds=["QCD1000001"], area=areas["Order Doors"])
    row = db.execute(select(JobPartNeeded).where(JobPartNeeded.job_id == str(job.id))
                     .where(JobPartNeeded.notes.like("%QCD1000001%"))).scalar_one()
    row.status = "received"
    db.commit()
    rec = _apply(db, "QCD1000001")
    assert rec.parts_marked == 0
    assert _parts(db, job)["QCD1000001"] == "received"
    # Nothing is still needed, so the job does leave the order lane.
    assert rec.moved_to_area == "Waiting on doors"


def test_door_order_status_counts_captured_doors_only(db, areas):
    job, _ = _sold_job(db, qcds=["QCD1000001", "QCD1000002"], area=areas["Order Doors"])
    assert door_order_status_for_jobs(db, [job.id]) == {str(job.id): {"total": 2, "ordered": 0}}
    _apply(db, "QCD1000001")
    assert door_order_status_for_jobs(db, [job.id]) == {str(job.id): {"total": 2, "ordered": 1}}
    labor_only, _ = _sold_job(db, qcds=[], area=None)
    assert door_order_status_for_jobs(db, [labor_only.id]) == {}


# --------------------------------------------------------------------------- #
# the mailbox pass
# --------------------------------------------------------------------------- #
class _FakeGraph:
    def __init__(self, bodies):
        self.bodies = bodies
        self.calls: list[str] = []

    def get_message(self, message_id):
        self.calls.append(message_id)
        return {"body": {"contentType": "html", "content": self.bodies[message_id]}}


def _account(db):
    acc = OutlookAccount(user_id="user-1", upn="office@example.com")
    db.add(acc)
    db.commit()
    return acc


def _msg(db, account, *, gid, received, subject="Order submission from SubDealer X1",
         sender="hubx@chiohd.com", mid=None):
    m = OutlookMessage(account_id=account.id, graph_message_id=gid, subject=subject,
                       internet_message_id=mid or f"<{gid}@mail.example>",
                       from_address=sender, to_addresses=[], body_preview="",
                       received_at=received, direction="inbound")
    db.add(m)
    db.commit()
    return m


def test_mailbox_pass_applies_once_and_never_reflips(db, areas):
    job, _ = _sold_job(db, qcds=["QCD1000001", "QCD1000002"], area=areas["Order Doors"])
    acc = _account(db)
    _msg(db, acc, gid="g-new", received=NOW - timedelta(days=1))
    _msg(db, acc, gid="g-old", received=NOW - timedelta(days=90))
    _msg(db, acc, gid="g-other", received=NOW, subject="Quote saved")
    gc = _FakeGraph({"g-new": TWO_DOOR_HTML})

    totals = process_hubx_orders(db, gc, acc, now=NOW)
    assert gc.calls == ["g-new"], "old mail and non-submissions are never fetched"
    assert totals["items"] == 2 and totals["parts_marked"] == 2 and totals["jobs_moved"] == 1
    db.refresh(job)
    assert job.holding_area_id == str(areas["Waiting on doors"].id)

    # The office sets one door back by hand (order cancelled). The next sync
    # must not flip it again, and must not re-fetch the message.
    row = db.execute(select(JobPartNeeded).where(JobPartNeeded.job_id == str(job.id))
                     .where(JobPartNeeded.notes.like("%QCD1000001%"))).scalar_one()
    row.status = "needed"
    db.commit()
    again = process_hubx_orders(db, gc, acc, now=NOW)
    assert again["messages"] == 0 and gc.calls == ["g-new"]
    assert _parts(db, job)["QCD1000001"] == "needed"
    assert len(db.execute(select(HubxDoorOrder)).scalars().all()) == 2


def test_mailbox_pass_survives_a_failing_message(db, areas):
    job, _ = _sold_job(db, qcds=["QCD1000001", "QCD1000002"], area=areas["Order Doors"])
    acc = _account(db)
    _msg(db, acc, gid="g-bad", received=NOW - timedelta(days=2))
    _msg(db, acc, gid="g-good", received=NOW - timedelta(days=1))

    class _Flaky(_FakeGraph):
        def get_message(self, message_id):
            if message_id == "g-bad":
                raise RuntimeError("graph down")
            return super().get_message(message_id)

    totals = process_hubx_orders(db, _Flaky({"g-good": TWO_DOOR_HTML}), acc, now=NOW)
    assert totals["errors"] == 1 and totals["parts_marked"] == 2
    # Nothing was recorded for the failed message, so the next sync retries it.
    gids = {r.graph_message_id for r in db.execute(select(HubxDoorOrder)).scalars()}
    assert gids == {"g-good"}


# --------------------------------------------------------------------------- #
# the leads page
# --------------------------------------------------------------------------- #
def test_lead_progress_carries_the_door_count(db, areas):
    from gdx_dispatch.models.tenant_models import Lead
    from gdx_dispatch.routers.leads import _progress_for_leads

    job, est = _sold_job(db, qcds=["QCD1000001", "QCD1000002"], area=areas["Order Doors"])
    lead = Lead(company_id=TID, name="Example Lead", stage="won", selected_estimate_id=est.id)
    db.add(lead)
    db.flush()
    est.lead_id = lead.id
    db.commit()

    before = _progress_for_leads(db, [lead])[str(lead.id)]
    assert before["doors"] == {"total": 2, "ordered": 0}
    _apply(db, "QCD1000001")
    _apply(db, "QCD1000002")
    after = _progress_for_leads(db, [lead])[str(lead.id)]
    assert after["doors"] == {"total": 2, "ordered": 2}


ONE_DOOR_HTML = TWO_DOOR_HTML.split("<h3>Item 2</h3>")[0] + "</body></html>"
NO_ITEM_HTML = "<html><body><p>Your cart was saved.</p></body></html>"


def test_a_message_is_fetched_once_even_when_it_adds_nothing(db, areas):
    """Resubmitted carts and item-less mail used to be re-fetched every sync."""
    _sold_job(db, qcds=["QCD1000001", "QCD1000002"], area=areas["Order Doors"])
    acc = _account(db)
    _msg(db, acc, gid="g-first", received=NOW - timedelta(days=3))
    _msg(db, acc, gid="g-resubmit", received=NOW - timedelta(days=2))
    _msg(db, acc, gid="g-empty", received=NOW - timedelta(days=1))
    gc = _FakeGraph({"g-first": TWO_DOOR_HTML, "g-resubmit": TWO_DOOR_HTML, "g-empty": NO_ITEM_HTML})

    for _ in range(3):
        process_hubx_orders(db, gc, acc, now=NOW)
    assert gc.calls == ["g-first", "g-resubmit", "g-empty"]
    rows = {r.graph_message_id: r for r in db.execute(select(HubxOrderEmail)).scalars()}
    assert (rows["g-resubmit"].item_count, rows["g-resubmit"].new_item_count) == (2, 0)
    assert rows["g-empty"].item_count == 0


def test_a_moved_message_is_not_read_again(db, areas):
    """Graph ids change on a folder move; the Message-ID does not."""
    _sold_job(db, qcds=["QCD1000001", "QCD1000002"], area=areas["Order Doors"])
    acc = _account(db)
    _msg(db, acc, gid="g-inbox", received=NOW - timedelta(days=1), mid="<same@mail.example>")
    gc = _FakeGraph({"g-inbox": TWO_DOOR_HTML, "g-archive": TWO_DOOR_HTML})
    process_hubx_orders(db, gc, acc, now=NOW)
    _msg(db, acc, gid="g-archive", received=NOW - timedelta(days=1), mid="<same@mail.example>")
    process_hubx_orders(db, gc, acc, now=NOW)
    assert gc.calls == ["g-inbox"]


def test_old_no_op_mail_cannot_starve_a_new_order(db, areas):
    job, _ = _sold_job(db, qcds=["QCD1000001", "QCD1000002"], area=areas["Order Doors"])
    acc = _account(db)
    bodies = {}
    for i in range(5):
        gid = f"g-noise-{i}"
        _msg(db, acc, gid=gid, received=NOW - timedelta(days=10, minutes=i))
        bodies[gid] = NO_ITEM_HTML
    _msg(db, acc, gid="g-real", received=NOW - timedelta(days=1))
    bodies["g-real"] = TWO_DOOR_HTML
    gc = _FakeGraph(bodies)

    process_hubx_orders(db, gc, acc, now=NOW, max_messages=3)
    process_hubx_orders(db, gc, acc, now=NOW, max_messages=3)
    assert "g-real" in gc.calls
    assert _parts(db, job)["QCD1000001"] == "ordered"


def _tier_job(db, *, qcds, area):
    """A tier-accepted estimate: doors on the estimate lines, but the job's
    parts rows come from the tier copy and carry no Number=."""
    job, est = _sold_job(db, qcds=qcds, area=area, labor=False)
    for p in db.execute(select(JobPartNeeded).where(JobPartNeeded.job_id == str(job.id))).scalars():
        db.delete(p)
    db.add(JobPartNeeded(id=str(uuid4()), company_id=TID, job_id=str(job.id),
                         part_name="Better package", quantity=1, status="needed",
                         notes="Accepted tier • $3000.00"))
    db.commit()
    return job


def test_a_two_door_tier_job_waits_for_both_doors(db, areas):
    job = _tier_job(db, qcds=["QCD1000001", "QCD1000002"], area=areas["Order Doors"])
    first = _apply(db, "QCD1000001")
    assert first.moved_to_area is None
    db.refresh(job)
    assert job.holding_area_id == str(areas["Order Doors"].id)
    second = _apply(db, "QCD1000002")
    assert second.moved_to_area == "Waiting on doors"


def test_a_door_set_back_to_needed_keeps_the_job_in_the_lane(db, areas):
    job, _ = _sold_job(db, qcds=["QCD1000001", "QCD1000002"], area=areas["Order Doors"])
    _apply(db, "QCD1000001")
    row = db.execute(select(JobPartNeeded).where(JobPartNeeded.job_id == str(job.id))
                     .where(JobPartNeeded.notes.like("%QCD1000001%"))).scalar_one()
    row.status = "needed"
    db.commit()
    assert _apply(db, "QCD1000002").moved_to_area is None


def test_an_order_emailed_before_acceptance_applies_once_accepted(db, areas):
    """On production the office often submits the HubX cart, then accepts the
    estimate minutes later — the email is read while no accepted estimate
    carries the QCD. The next sync must pick it up without re-fetching."""
    job, est = _sold_job(db, qcds=["QCD1000001", "QCD1000002"], area=areas["Order Doors"], status="sent")
    acc = _account(db)
    _msg(db, acc, gid="g-early", received=NOW - timedelta(hours=1))
    gc = _FakeGraph({"g-early": TWO_DOOR_HTML})

    first = process_hubx_orders(db, gc, acc, now=NOW)
    assert first["items"] == 2 and first["parts_marked"] == 0
    assert {r.outcome for r in db.execute(select(HubxDoorOrder)).scalars()} == {"no_estimate"}

    est.status = "accepted"
    db.commit()
    second = process_hubx_orders(db, gc, acc, now=NOW)
    assert gc.calls == ["g-early"], "the retry needs no Graph call"
    assert second["resolved"] == 2 and second["parts_marked"] == 2 and second["jobs_moved"] == 1
    db.refresh(job)
    assert job.holding_area_id == str(areas["Waiting on doors"].id)
    assert {r.outcome for r in db.execute(select(HubxDoorOrder)).scalars()} == {"applied"}


def test_unmatched_orders_outside_the_window_are_not_retried(db, areas):
    _sold_job(db, qcds=["QCD1000001"], area=areas["Order Doors"], status="sent")
    rec = _apply(db, "QCD1000001")
    # Recorded recently (a first deploy reading backlog), but the EMAIL is old.
    rec.received_at = NOW - timedelta(days=45)
    rec.created_at = NOW - timedelta(days=1)
    db.commit()
    est = db.execute(select(Estimate)).scalars().one()
    est.status = "accepted"
    db.commit()
    acc = _account(db)
    out = process_hubx_orders(db, _FakeGraph({}), acc, now=NOW)
    assert out["retried"] == 0
    db.refresh(rec)
    assert rec.outcome == "no_estimate"
