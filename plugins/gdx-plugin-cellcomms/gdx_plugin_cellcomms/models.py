"""Cell-comms plugin tables — namespaced plug_cellcomms_* per ADR-013.

Inherit PluginBase so the plugin-host migration phase sees them on one metadata.
Two feeds write here: the android-nomad-gateway webhook (live, incoming only)
and the SMS Backup & Restore XML backfill (both directions, full history).
`dedupe_key` is derived from message-intrinsic fields — never a delivery id —
so the two feeds upsert the same real-world event into one row.

`customer_id` is a plain string (canonical dashed UUID), not a ForeignKey:
plugin tables stay decoupled from core metadata, and a raw cross-dialect join
on a `Uuid` column is the known SQLite dashless-hex trap. `customer_name` is a
display snapshot taken at match time; the rematch pass refreshes it.
"""
from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, String, Text, func

from gdx_dispatch.plugin_api.base import PluginBase


class CellMessage(PluginBase):
    __tablename__ = "plug_cellcomms_messages"

    id = Column(Integer, primary_key=True)
    company_id = Column(String(64), nullable=False, index=True)
    direction = Column(String(3), nullable=False)  # in / out
    other_number = Column(String(40), nullable=True, index=True)  # E.164 when parseable
    body = Column(Text, nullable=True)
    sim = Column(String(20), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True, index=True)
    customer_id = Column(String(36), nullable=True, index=True)
    customer_name = Column(String(200), nullable=True)
    source = Column(String(10), nullable=False)  # webhook / backfill
    dedupe_key = Column(String(64), unique=True, nullable=False)
    raw_payload = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# CellCall carries a second key column CellMessage doesn't need: a webhook row
# ENRICHED by a backfill element must remember that element's key, or the next
# re-upload of the same backup re-imports the call as a fresh row (2026-09-18
# re-audit finding 1). Messages only ever absorb-and-skip, which repeats
# harmlessly; calls mutate, so the marker must persist.
class CellCall(PluginBase):
    __tablename__ = "plug_cellcomms_calls"

    id = Column(Integer, primary_key=True)
    company_id = Column(String(64), nullable=False, index=True)
    direction = Column(String(3), nullable=False)  # in / out
    call_type = Column(String(10), nullable=True)  # incoming/outgoing/missed/rejected/voicemail
    other_number = Column(String(40), nullable=True, index=True)
    contact_name = Column(String(120), nullable=True)  # the PHONE's contact label, not ours
    started_at = Column(DateTime(timezone=True), nullable=True, index=True)
    duration_s = Column(Integer, nullable=True)
    customer_id = Column(String(36), nullable=True, index=True)
    customer_name = Column(String(200), nullable=True)
    source = Column(String(10), nullable=False)
    dedupe_key = Column(String(64), unique=True, nullable=False)
    # The backup element's key, recorded at enrichment (NULL = never enriched).
    backfill_key = Column(String(64), unique=True, nullable=True)
    raw_payload = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
