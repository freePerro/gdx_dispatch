"""Tests for the browser-stream session recorder.

The guards that matter here are the ones that can fail for a real defect:
a recorder that records nothing while looking healthy, one that leaks the
operator's typed password, or one that takes the quoting session down with it.
"""

import asyncio
import json
import os
from pathlib import Path

import pytest

from gdx_dispatch.core.session_recorder import SessionRecorder


def _rec(tmp_path, **kw):
    return SessionRecorder(
        actor="u1", plugin_key="chipricing",
        url="https://portal.example.invalid/", root=str(tmp_path), **kw
    )


async def _drain(rec):
    """Let the writer task run, then close and return the parsed events."""
    await asyncio.sleep(0.05)
    rec.close_sync("test")
    lines = (rec.dir / "events.jsonl").read_text().splitlines()
    return [json.loads(x) for x in lines if x.strip()]


@pytest.mark.asyncio
async def test_records_a_session_end_to_end(tmp_path):
    rec = _rec(tmp_path)
    await rec.start()
    rec.note_client(json.dumps({"type": "nav", "url": "https://portal.example.invalid/cart"}))
    rec.note_server(json.dumps({"type": "capture", "url": "u", "text": "Door Summary\n$1,293.93"}))
    events = await _drain(rec)

    kinds = [e["t"] for e in events]
    assert "session_start" in kinds
    assert "capture" in kinds
    assert "session_end" in kinds
    # The capture body is kept whole and unredacted — it is the quote data.
    body = (rec.dir / "capture-001.txt").read_text()
    assert "$1,293.93" in body


@pytest.mark.asyncio
async def test_typed_text_is_recorded_as_length_never_content(tmp_path):
    """Counterfactual: if this stored the string, the assertion below fails."""
    rec = _rec(tmp_path)
    await rec.start()
    secret = "hunter2-not-a-real-password"
    rec.note_client(json.dumps({"type": "text", "text": secret}))
    rec.note_client(json.dumps({"type": "key", "payload": {"type": "keyDown", "text": "x"}}))
    events = await _drain(rec)

    blob = (rec.dir / "events.jsonl").read_text()
    assert secret not in blob
    text_ev = [e for e in events if e.get("kind") == "text"][0]
    assert text_ev["len"] == len(secret)
    assert "text" not in text_ev


@pytest.mark.asyncio
async def test_saved_session_keeps_cookie_names_but_never_values(tmp_path):
    rec = _rec(tmp_path)
    await rec.start()
    rec.note_server(json.dumps({
        "type": "session",
        "state": {"cookies": [
            {"domain": "portal.example.invalid", "name": "PortalAuth.Prod",
             "value": "SUPERSECRETTICKETVALUE"},
        ]},
    }))
    events = await _drain(rec)

    blob = (rec.dir / "events.jsonl").read_text()
    assert "SUPERSECRETTICKETVALUE" not in blob
    ev = [e for e in events if e["t"] == "session_saved"][0]
    assert ev["cookie_count"] == 1
    assert "portal.example.invalid|PortalAuth.Prod" in ev["cookie_names"]


@pytest.mark.asyncio
async def test_truncated_capture_is_flagged(tmp_path):
    """The plugin-host caps capture text on the wire; a reader must not mistake
    a cut-off price table for the whole page."""
    rec = _rec(tmp_path)
    await rec.start()
    rec.note_server(json.dumps({"type": "capture", "url": "u", "text": "x" * 500_000}))
    events = await _drain(rec)
    cap = [e for e in events if e["t"] == "capture"][0]
    assert cap["truncated"] is True


@pytest.mark.asyncio
async def test_recorder_faults_degrade_and_never_raise(tmp_path):
    """A recorder fault must not end the operator's quoting session."""
    rec = _rec(tmp_path)
    await rec.start()
    # Malformed input from either side is recorded, not raised.
    rec.note_client("this is not json")
    rec.note_server("neither is this")
    assert rec.degraded is False
    events = await _drain(rec)
    assert any(e["t"] == "in_unparsed" for e in events)
    assert any(e["t"] == "out_unparsed" for e in events)


@pytest.mark.asyncio
async def test_unwritable_directory_degrades_loudly_and_does_not_raise(tmp_path):
    """No silent fallback to a temp dir that evaporates on the next deploy."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    os.chmod(blocked, 0o500)  # read+execute, not writable
    try:
        rec = _rec(blocked)
        await rec.start()  # must not raise
        assert rec.enabled is False
        assert rec.degraded is True
        assert rec.stats()["recording"] is False
        # And it still absorbs traffic without raising.
        rec.note_client(json.dumps({"type": "mouse", "payload": {}}))
    finally:
        os.chmod(blocked, 0o700)


@pytest.mark.asyncio
async def test_stats_report_recording_only_after_bytes_hit_disk(tmp_path):
    """The REC badge is driven by this; it must be able to say 'not recording'."""
    rec = _rec(tmp_path)
    assert rec.stats()["recording"] is False  # before start
    await rec.start()
    rec.note_server(json.dumps({"type": "capture", "url": "u", "text": "Door Summary"}))
    await asyncio.sleep(0.05)
    s = rec.stats()
    assert s["recording"] is True
    assert s["bytes"] > 0
    assert s["captures"] == 1
    rec.close_sync("test")


@pytest.mark.asyncio
async def test_frames_are_sampled_not_stored_wholesale(tmp_path):
    """Full-rate frames are 1-2 GB/hour and answer nothing the page text doesn't."""
    import base64
    rec = _rec(tmp_path)
    await rec.start()
    frame = base64.b64encode(b"\xff\xd8\xff" + b"0" * 500).decode()
    for _ in range(50):
        rec.note_server(json.dumps({"type": "frame", "data": frame}))
    await asyncio.sleep(0.05)
    rec.close_sync("test")
    kept = list((rec.dir / "frames").glob("*.jpg"))
    assert 0 < len(kept) < 50, f"expected sampling, kept {len(kept)}/50"


@pytest.mark.asyncio
async def test_manifest_and_index_are_written_for_a_human(tmp_path):
    rec = _rec(tmp_path)
    await rec.start()
    rec.note_server(json.dumps({"type": "capture", "url": "u", "text": "Door Summary"}))
    await asyncio.sleep(0.05)
    rec.close_sync("done")

    manifest = json.loads((rec.dir / "manifest.json").read_text())
    assert manifest["sid"] == rec.sid
    assert manifest["actor"] == "u1"
    assert manifest["captures"] == 1
    assert manifest["reason"] == "done"
    # A root index so the corpus is greppable without opening every directory.
    assert rec.sid in (Path(rec.root) / "INDEX.txt").read_text()


@pytest.mark.asyncio
async def test_close_is_idempotent(tmp_path):
    rec = _rec(tmp_path)
    await rec.start()
    a = rec.close_sync("first")
    b = rec.close_sync("second")
    assert a["sid"] == b["sid"]
    ends = [json.loads(x) for x in (rec.dir / "events.jsonl").read_text().splitlines()
            if '"session_end"' in x]
    assert len(ends) == 1


@pytest.mark.asyncio
async def test_credential_bearing_urls_are_scrubbed(tmp_path):
    """An OIDC return can carry a bearer credential in the URL, and URLs are
    written to disk and then copied to a laptop."""
    from gdx_dispatch.core.session_recorder import scrub_url

    dirty = "https://portal.example.invalid/signin-oidc?code=SECRETAUTHCODE&state=abc"
    assert "SECRETAUTHCODE" not in scrub_url(dirty)
    assert "portal.example.invalid/signin-oidc" in scrub_url(dirty)
    frag = "https://x/cb#id_token=SECRETJWT&scope=openid"
    assert "SECRETJWT" not in scrub_url(frag)
    assert "scope=openid" in scrub_url(frag)
    # A normal portal URL must survive intact — over-scrubbing loses the trail.
    plain = "https://portal.example.invalid/cart/Quote/3/abc/door/view/def"
    assert scrub_url(plain) == plain

    rec = _rec(tmp_path)
    await rec.start()
    rec.note_client(json.dumps({"type": "nav", "url": dirty}))
    rec.note_server(json.dumps({"type": "capture", "url": dirty, "text": "Door Summary"}))
    await _drain(rec)
    blob = (rec.dir / "events.jsonl").read_text() + (rec.dir / "manifest.json").read_text()
    assert "SECRETAUTHCODE" not in blob


@pytest.mark.asyncio
async def test_sustained_backpressure_degrades_instead_of_lying(tmp_path):
    """A byte cap that drops silently would report a healthy recording while
    everything went in the bin."""
    from gdx_dispatch.core import session_recorder as sr

    rec = _rec(tmp_path)
    await rec.start()
    big = "x" * 200_000
    orig = sr._MAX_QUEUED_BYTES
    sr._MAX_QUEUED_BYTES = 1000  # force the cap without writing 32MB
    try:
        for _ in range(150):
            rec.note_server(json.dumps({"type": "capture", "url": "u", "text": big}))
    finally:
        sr._MAX_QUEUED_BYTES = orig
    assert rec.dropped > 0
    assert rec.degraded is True
    assert rec.stats()["recording"] is False  # the badge must show this
    rec.close_sync("test")
