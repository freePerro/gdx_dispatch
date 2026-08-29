"""Full-detail recording of plugin browser-stream sessions.

Why this exists
---------------
Some supplier portals expose no usable API, so the only way to work with one is
to drive its real UI in a browser — which is what the plugin browser-stream
already does. But today a plugin keeps only a single snapshot per capture and
the rest of the session is discarded: every intermediate configuration, and
every price recomputation as options are toggled, is lost.

This records the whole session.

Where it hooks
--------------
The core proxy (`routers/browser_proxy.py`) is a verbatim relay between the
operator's browser and the plugin-host's Chromium. It already sees every input
and every server message, it runs in `app` where the ticket's `sub` is already
decoded, and — critically — it is NOT the ack-gated frame loop inside
`plugin_host/browser_stream.py`. Recording here cannot freeze the operator's
live view, and adds no parameter to a signature frozen by tests.

Design rules (each one is load-bearing)
---------------------------------------
* **Never break the session.** Every public method swallows its own exceptions
  and sets `degraded`. A recorder fault must never end a quoting session.
* **Never block the relay.** `note_*` only does `put_nowait` on a bounded queue;
  all I/O happens in a writer task via `asyncio.to_thread`.
* **Never fall back silently.** An unusable directory is a loud WARNING and a
  `degraded` flag the UI can show — not a quiet switch to a temp dir that
  evaporates on the next deploy.
* **Never record secrets.** Typed text and keystrokes are recorded as
  `{"len": N}` only; saved-session replies keep cookie *names* but never
  values; and URLs are stripped of credential-bearing params before they touch
  disk (an OIDC return can carry `?code=` / `#id_token=`). The operator types
  their portal password into this stream, and the corpus is copied to an
  operator's machine — so redaction happens at the source, not in a later pass.

  **Known limitation, stated plainly:** `document.body.innerText` does NOT
  include `<input>`/`<textarea>` *values*, so a dimension or quantity the
  operator *types* is recorded only as a length. Values the remote page renders
  as text are captured in full, which on a summary page is typically the whole
  configuration. The gap is mid-configuration form state, and closing it needs a
  DOM snapshot that reads input values, not a relaxation of this redaction. Do
  not assume a complete input->price mapping until that lands.
* **Tolerate a torn tail.** Append-only JSONL, fsynced on the events that
  matter, so a SIGKILL costs the last line and not the session.
* **Drops are visible.** Back-pressure writes a `dropped` marker and increments
  a counter that reaches both the live badge and the closing manifest. A gap the
  reader cannot see is worse than a gap.

The quote data itself is deliberately kept whole and unredacted — prices, job
names, dealer detail. That is the point of the corpus.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger(__name__)

# Recording is opt-out, but the directory must exist and be writable — see
# _prepare(). Default lives on its own volume, deliberately NOT under
# /app/uploads: that tree is what the DB-backup routine takes off-box
# (routers/admin_db.py writes into /app/uploads/_db_backups), and an unscrubbed
# corpus must not ride along in every backup artifact that leaves the machine.
_DEFAULT_ROOT = "/app/recordings"

# Back-pressure bounds. Bounded by BOTH count and bytes: 2000 slots holding
# page-text captures at ~40KB each is 80MB resident, and this container has no
# memory limit while Chromium next door already leaks by design.
_MAX_QUEUED_ITEMS = 2000
_MAX_QUEUED_BYTES = 32 * 1024 * 1024

# Frame sampling. The screencast runs at whatever rate Chromium emits; storing
# every frame is 1-2 GB/hour and answers no question the page text doesn't.
# Keep a slow heartbeat of frames plus a burst around anything interesting.
_FRAME_MIN_INTERVAL_S = 2.0
_FRAME_AFTER_EVENT_S = 1.0

# Disk floors. Filling the volume is a production outage (Postgres and Redis
# share the device), so refuse to start well before that and stop in flight.
_FREE_REFUSE_START_GB = 20.0
_FREE_STOP_RECORDING_GB = 10.0

# The plugin-host truncates capture text at this length on the wire; when we see
# exactly this many characters the page was longer than what we recorded, and
# the longest pages are the itemised price tables that matter most.
_CAPTURE_WIRE_LIMIT = 500_000

# Query/fragment keys that carry a credential rather than a location. A portal's
# SSO may deliver tokens in a POST body rather than the URL, but that is a
# property of one deployment's config and not a guarantee — and a URL here is
# written to disk and then copied to an operator's machine.
_URL_SECRET_KEYS = ("code", "id_token", "access_token", "token", "refresh_token",
                    "client_secret", "session_state", "state", "ticket")


def scrub_url(url: str) -> str:
    """Drop credential-bearing query/fragment params, keep the location."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
        if not (parts.query or parts.fragment):
            return url

        def _clean(blob: str) -> str:
            out = []
            for piece in blob.split("&"):
                key = piece.split("=", 1)[0].lower()
                out.append(f"{key}=<redacted>" if key in _URL_SECRET_KEYS else piece)
            return "&".join(out)

        return urlunsplit((parts.scheme, parts.netloc, parts.path,
                           _clean(parts.query), _clean(parts.fragment)))
    except Exception:
        return "<unparsable-url>"


def recording_root() -> str:
    return os.environ.get("GDX_SESSION_RECORDING_DIR") or _DEFAULT_ROOT


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _free_gb(path: str) -> float:
    try:
        return shutil.disk_usage(path).free / (1024**3)
    except Exception:
        return -1.0


class SessionRecorder:
    """Records one browser-stream session to its own directory.

    Lifecycle: ``await start()`` → ``note_client``/``note_server`` per relayed
    message → ``close_sync(reason)`` from the proxy's finally block.
    """

    def __init__(
        self,
        *,
        actor: str,
        plugin_key: str,
        url: str,
        session_id: str | None = None,
        root: str | None = None,
    ) -> None:
        self.sid = session_id or uuid.uuid4().hex
        self.actor = actor or "unknown"
        self.plugin_key = plugin_key or ""
        self.url = url or ""
        self.root = root or recording_root()
        self.dir = Path(self.root) / datetime.now(timezone.utc).strftime("%Y-%m-%d") / self.sid

        self.enabled = False
        self.degraded = False
        self.degraded_reason = ""
        self.events = 0
        self.bytes_written = 0
        self.dropped = 0
        self.captures = 0
        self.frames_kept = 0
        self.started_at = time.time()

        self._q: asyncio.Queue | None = None
        self._queued_bytes = 0
        self._writer: asyncio.Task | None = None
        self._fh = None
        self._fh_lock = threading.Lock()
        self._frame_seq = 0
        self._last_frame_at = 0.0
        self._interesting_until = 0.0
        self._urls: list[str] = []
        self._qcds: list[str] = []
        self._closed = False

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Prepare the directory and start the writer. Never raises."""
        try:
            self._prepare()
        except Exception as e:
            self._degrade(f"could not prepare {self.dir}: {e}")
            return
        self._q = asyncio.Queue(maxsize=_MAX_QUEUED_ITEMS)
        self._writer = asyncio.create_task(self._writer_loop())
        self.enabled = True
        self._emit(
            {
                "t": "session_start",
                "sid": self.sid,
                "actor": self.actor,
                "plugin_key": self.plugin_key,
                "url": scrub_url(self.url),
                "ts": _now_iso(),
                "free_gb": round(_free_gb(self.root), 1),
            }
        )
        log.info("session-recorder started sid=%s dir=%s", self.sid, self.dir)

    def _prepare(self) -> None:
        free = _free_gb(self.root if os.path.isdir(self.root) else "/")
        if 0 <= free < _FREE_REFUSE_START_GB:
            raise RuntimeError(f"only {free:.1f}GB free, need {_FREE_REFUSE_START_GB}GB")
        # No silent fallback: if this fails the caller degrades loudly.
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "frames").mkdir(exist_ok=True)
        # noqa SIM115 is deliberate: this handle is held open for the life of the
        # session and closed in close_sync(); a context manager cannot span that.
        self._fh = open(self.dir / "events.jsonl", "a", encoding="utf-8")  # noqa: SIM115

    def _degrade(self, reason: str) -> None:
        self.enabled = False
        self.degraded = True
        self.degraded_reason = reason
        log.warning("session-recorder DEGRADED sid=%s: %s", self.sid, reason)

    # ── the relay taps ───────────────────────────────────────────────────────

    def note_client(self, raw: str) -> None:
        """One operator→browser message. Must never raise, never block."""
        if not self.enabled:
            return
        try:
            ev = json.loads(raw)
        except Exception:
            self._emit({"t": "in_unparsed", "len": len(raw), "ts": _now_iso()})
            return
        try:
            self._note_client(ev)
        except Exception as e:
            self._degrade(f"note_client: {e}")

    def _note_client(self, ev: dict) -> None:
        kind = ev.get("type")
        now = time.time()
        rec: dict[str, Any] = {"t": "in", "kind": kind, "ts": _now_iso()}

        if kind == "text":
            # Length only — never the characters. The operator may be typing a
            # password into the portal's sign-in form, and the snapshots already
            # capture every field value this typing produces.
            rec["len"] = len(str(ev.get("text", "")))
            self._interesting_until = now + _FRAME_AFTER_EVENT_S
        elif kind == "key":
            p = ev.get("payload") or {}
            rec["key_type"] = p.get("type")
            rec["len"] = len(str(p.get("text", "")))
            self._interesting_until = now + _FRAME_AFTER_EVENT_S
        elif kind == "mouse":
            p = ev.get("payload") or {}
            rec["mouse"] = {k: p.get(k) for k in ("type", "x", "y", "button", "clickCount")}
            if p.get("type") in ("mousePressed", "mouseReleased"):
                self._interesting_until = now + _FRAME_AFTER_EVENT_S
        elif kind == "nav":
            rec["url"] = scrub_url(str(ev.get("url") or ""))
            self._urls.append(rec["url"])
            self._interesting_until = now + _FRAME_AFTER_EVENT_S
        elif kind in ("capture", "save_session", "close"):
            self._interesting_until = now + _FRAME_AFTER_EVENT_S

        self._emit(rec)

    def note_server(self, raw: str) -> None:
        """One browser→operator message. Must never raise, never block."""
        if not self.enabled:
            return
        try:
            ev = json.loads(raw)
        except Exception:
            self._emit({"t": "out_unparsed", "len": len(raw), "ts": _now_iso()})
            return
        try:
            self._note_server(ev)
        except Exception as e:
            self._degrade(f"note_server: {e}")

    def _note_server(self, ev: dict) -> None:
        kind = ev.get("type")

        if kind == "frame":
            self._note_frame(ev)
            return

        if kind == "capture":
            text = str(ev.get("text") or "")
            self.captures += 1
            name = f"capture-{self.captures:03d}"
            # Kept whole and unredacted: this is the quote data, the entire
            # reason the corpus exists.
            self._queue_blob(f"{name}.txt", text.encode("utf-8"))
            img = ev.get("image")
            if isinstance(img, str) and img:
                self._queue_blob(f"{name}.img.txt", img.encode("utf-8"))
            self._emit(
                {
                    "t": "capture",
                    "n": self.captures,
                    "url": scrub_url(str(ev.get("url") or "")),
                    "text_len": len(text),
                    # The plugin-host caps capture text on the wire; flag it so a
                    # reader never mistakes a cut-off price table for the page.
                    "truncated": len(text) >= _CAPTURE_WIRE_LIMIT,
                    "has_image": bool(img),
                    "file": f"{name}.txt",
                    "ts": _now_iso(),
                },
                important=True,
            )
            return

        if kind == "session":
            # Cookie NAMES only. The values are bearer-equivalent credentials.
            state = ev.get("state") or {}
            cookies = state.get("cookies") or []
            names = []
            for c in cookies:
                if isinstance(c, dict):
                    names.append(f"{c.get('domain', '')}|{c.get('name', '')}")
            self._emit(
                {"t": "session_saved", "cookie_count": len(cookies),
                 "cookie_names": names, "ts": _now_iso()},
                important=True,
            )
            return

        self._emit({"t": "out", "kind": kind, "ts": _now_iso()})

    def _note_frame(self, ev: dict) -> None:
        now = time.time()
        interesting = now <= self._interesting_until
        due = (now - self._last_frame_at) >= _FRAME_MIN_INTERVAL_S
        if not (interesting or due):
            return
        data = ev.get("data")
        if not isinstance(data, str) or not data:
            return
        self._last_frame_at = now
        self._frame_seq += 1
        name = f"frames/{self._frame_seq:05d}.jpg"
        try:
            blob = base64.b64decode(data)
        except Exception:
            return
        self._queue_blob(name, blob)
        self.frames_kept += 1
        self._emit({"t": "frame", "n": self._frame_seq, "file": name,
                    "bytes": len(blob), "ts": _now_iso()})

    # ── queueing ─────────────────────────────────────────────────────────────

    def _emit(self, obj: dict, *, important: bool = False) -> None:
        if self._q is None:
            return
        try:
            line = json.dumps(obj, default=str, ensure_ascii=False) + "\n"
        except Exception as e:
            self._degrade(f"encode: {e}")
            return
        self._put(("line", line.encode("utf-8"), important))

    def _queue_blob(self, name: str, blob: bytes) -> None:
        self._put(("blob", (name, blob), False))

    def _put(self, item: tuple) -> None:
        if self._q is None:
            return
        size = len(item[1]) if item[0] == "line" else len(item[1][1])
        if self._queued_bytes + size > _MAX_QUEUED_BYTES:
            # Sustained back-pressure, not a blip: say so, or stats() would keep
            # reporting a healthy recording while everything is going in the bin.
            self._drop()
            if self.dropped >= 100 and not self.degraded:
                self._degrade(f"queue over {_MAX_QUEUED_BYTES // (1024 * 1024)}MB — dropping")
            return
        try:
            self._q.put_nowait(item)
            self._queued_bytes += size
        except asyncio.QueueFull:
            self._drop()

    def _drop(self) -> None:
        # Visible gap. A reader must be able to tell "nothing happened" from
        # "we could not keep up".
        self.dropped += 1
        if self._q is not None and self.dropped % 50 == 1:
            # If even the marker cannot be queued the counter still carries the
            # drop into stats() and the closing manifest.
            with contextlib.suppress(asyncio.QueueFull):
                self._q.put_nowait(
                    ("line",
                     (json.dumps({"t": "dropped", "n": self.dropped,
                                  "ts": _now_iso()}) + "\n").encode("utf-8"),
                     True)
                )

    # ── writer ───────────────────────────────────────────────────────────────

    async def _writer_loop(self) -> None:
        assert self._q is not None
        while True:
            try:
                item = await self._q.get()
            except asyncio.CancelledError:
                return
            batch = [item]
            while True:
                try:
                    batch.append(self._q.get_nowait())
                except asyncio.QueueEmpty:
                    break
                if len(batch) >= 200:
                    break
            try:
                await asyncio.to_thread(self._write_batch, batch)
            except asyncio.CancelledError:
                self._write_batch(batch)  # last chance, synchronously
                return  # the finally below settles the byte accounting
            except Exception as e:
                self._degrade(f"writer: {e}")
                return
            finally:
                # On the event loop, single-threaded: no lost decrements.
                self._queued_bytes = max(0, self._queued_bytes - self._batch_bytes(batch))

    @staticmethod
    def _batch_bytes(batch: list[tuple]) -> int:
        return sum(len(p) if k == "line" else len(p[1]) for k, p, _ in batch)

    def _write_batch(self, batch: list[tuple]) -> None:
        """Runs on a worker thread (or synchronously during drain).

        Every touch of self._fh is under _fh_lock: close_sync() contains no
        awaits by design, so it cannot wait for an in-flight to_thread write and
        would otherwise close the handle out from under this.
        """
        important = False
        lines: list[str] = []
        for kind, payload, imp in batch:
            size = len(payload) if kind == "line" else len(payload[1])
            if kind == "line":
                lines.append(payload.decode("utf-8"))
                self.bytes_written += size
                self.events += 1
                important = important or imp
            else:
                name, blob = payload
                try:
                    p = self.dir / name
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(blob)
                    self.bytes_written += size
                except Exception as e:
                    self._degrade(f"blob {name}: {e}")
        if lines:
            with self._fh_lock:
                if self._fh is None:
                    return  # closed underneath us; the drain already ran
                self._fh.write("".join(lines))
                self._fh.flush()
                if important:
                    try:
                        os.fsync(self._fh.fileno())
                    except Exception:
                        self.degraded = True
        # Stop before the volume becomes a production incident.
        if _free_gb(self.root) >= 0 and _free_gb(self.root) < _FREE_STOP_RECORDING_GB:
            self._degrade("free space below floor — recording stopped")

    # ── status + close ───────────────────────────────────────────────────────

    def stats(self) -> dict:
        """What the live REC badge shows. Driven by bytes actually written."""
        return {
            "sid": self.sid,
            "recording": bool(self.enabled and not self.degraded),
            "events": self.events,
            "bytes": self.bytes_written,
            "captures": self.captures,
            "frames": self.frames_kept,
            "dropped": self.dropped,
            "degraded": self.degraded,
            "reason": self.degraded_reason,
        }

    def close_sync(self, reason: str = "closed") -> dict:
        """Drain and close. Contains ZERO awaits by design.

        The proxy's finally block is reached by CancelledError on every prod
        deploy (uvicorn SIGTERM); inside a cancelled finally the next await
        re-raises immediately and everything after it is skipped. So the drain
        is a plain get_nowait loop and the file is closed synchronously.
        """
        if self._closed:
            return self.stats()
        self._closed = True
        try:
            if self._writer is not None:
                self._writer.cancel()
            batch: list[tuple] = []
            if self._q is not None:
                while True:
                    try:
                        batch.append(self._q.get_nowait())
                    except asyncio.QueueEmpty:
                        break
            if batch:
                self._write_batch(batch)
            summary = {
                "t": "session_end",
                "sid": self.sid,
                "reason": reason,
                "duration_s": round(time.time() - self.started_at, 1),
                "events": self.events + 1,
                "captures": self.captures,
                "frames": self.frames_kept,
                "dropped": self.dropped,
                "degraded": self.degraded,
                "ts": _now_iso(),
            }
            with self._fh_lock:
                if self._fh is not None:
                    self._fh.write(json.dumps(summary) + "\n")
                    self._fh.flush()
                    try:
                        os.fsync(self._fh.fileno())
                    except Exception:
                        self.degraded = True
                    self._fh.close()
                    self._fh = None
            self._write_manifest(summary)
        except Exception as e:
            log.warning("session-recorder close failed sid=%s: %s", self.sid, e)
        log.info(
            "session-recorder closed sid=%s events=%s captures=%s dropped=%s degraded=%s",
            self.sid, self.events, self.captures, self.dropped, self.degraded,
        )
        return self.stats()

    def _write_manifest(self, summary: dict) -> None:
        """manifest.json for machines; INDEX.txt so a human can grep the pile."""
        try:
            manifest = {
                "sid": self.sid,
                "actor": self.actor,
                "plugin_key": self.plugin_key,
                "start_url": scrub_url(self.url),
                "urls": self._urls[:200],
                "qcds": self._qcds,
                **summary,
            }
            (self.dir / "manifest.json").write_text(json.dumps(manifest, indent=1))
            idx = (
                f"{summary['ts']}  sid={self.sid}  actor={self.actor}  "
                f"key={self.plugin_key}  dur={summary['duration_s']}s  "
                f"events={summary['events']}  captures={self.captures}  "
                f"frames={self.frames_kept}  dropped={self.dropped}"
                f"{'  DEGRADED=' + self.degraded_reason if self.degraded else ''}\n"
            )
            (self.dir / "INDEX.txt").write_text(idx)
            root_index = Path(self.root) / "INDEX.txt"
            with open(root_index, "a", encoding="utf-8") as fh:
                fh.write(idx)
        except Exception as e:
            log.warning("session-recorder manifest failed sid=%s: %s", self.sid, e)
