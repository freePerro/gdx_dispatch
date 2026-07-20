"""Security-logic tests for the plugin browser stream (ADR-014).

Covers the allowlist + SSRF navigation guard — the parts that, if wrong, let the
server browser egress to an arbitrary/internal host — and the remembered-login
session store (path containment + encryption at rest). Loaded by path so it
needs no DB/app context.
"""
import importlib.util
import json
import os
from pathlib import Path

_BS = Path(__file__).resolve().parents[1] / "plugin_host" / "browser_stream.py"
_spec = importlib.util.spec_from_file_location("browser_stream", _BS)
bs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bs)


def test_host_allowed_allowlist():
    assert bs.host_allowed("https://orderentry.chiohd.com/cart")
    assert not bs.host_allowed("https://evil.example.com/")


def test_host_allowed_rejects_userinfo_spoof():
    # urlparse resolves the real host after '@', so allowed@evil can't spoof.
    assert not bs.host_allowed("https://orderentry.chiohd.com@evil.example.com/")


def test_nav_guard_blocks_offlist_navigation_any_frame():
    # SSRF pivot via iframe/popup to cloud metadata must be blocked.
    assert bs.nav_should_block("http://169.254.169.254/latest/meta-data/", True)
    assert bs.nav_should_block("https://evil.example.com/", True)


def test_nav_guard_allows_allowlisted_navigation():
    assert not bs.nav_should_block("https://orderentry.chiohd.com/cart", True)


def test_nav_guard_allows_subresources():
    # Non-navigation requests (scripts, images, XHR) pass so pages render.
    assert not bs.nav_should_block("https://cdn.example.com/app.js", False)


def test_nav_guard_ignores_non_http_schemes():
    # about:/data:/blob: have no network host — no egress, so not blocked.
    assert not bs.nav_should_block("about:blank", True)
    assert not bs.nav_should_block("data:text/html,hi", True)


def test_state_file_sanitizes_key(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGIN_BROWSER_STATE_DIR", str(tmp_path))
    # A hostile key cannot traverse out of the state dir.
    p = bs.state_file_for("../../etc/passwd")
    assert p is not None and Path(p).parent == tmp_path
    assert bs.state_file_for("chipricing") == str(tmp_path / "chipricing.session")
    # Keys that sanitize to nothing yield no path → no persistence.
    assert bs.state_file_for("") is None
    assert bs.state_file_for("!!!") is None


def test_state_roundtrip_plaintext(tmp_path, monkeypatch):
    # Keyless dev: save/load round-trips, file is owner-only.
    monkeypatch.setattr(bs, "_fernet", lambda: None)
    path = str(tmp_path / "sub" / "chipricing.session")
    state = {"cookies": [{"name": "auth", "value": "abc"}], "origins": []}
    bs.save_state(path, state)
    assert bs.load_state(path) == state
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_state_roundtrip_encrypted(tmp_path, monkeypatch):
    # With a key, the bytes on disk are ciphertext, and load decrypts them.
    from cryptography.fernet import Fernet

    f = Fernet(Fernet.generate_key())
    monkeypatch.setattr(bs, "_fernet", lambda: f)
    path = str(tmp_path / "chipricing.session")
    state = {"cookies": [{"name": "auth", "value": "s3cret"}], "origins": []}
    bs.save_state(path, state)
    raw = Path(path).read_bytes()
    assert b"s3cret" not in raw
    assert bs.load_state(path) == state


def test_load_state_failures_return_none(tmp_path, monkeypatch):
    monkeypatch.setattr(bs, "_fernet", lambda: None)
    assert bs.load_state(str(tmp_path / "missing.session")) is None
    corrupt = tmp_path / "corrupt.session"
    corrupt.write_bytes(b"\x00not json")
    assert bs.load_state(str(corrupt)) is None
    # A non-dict JSON payload is rejected too (Playwright needs a dict).
    lst = tmp_path / "list.session"
    lst.write_text(json.dumps([1, 2]))
    assert bs.load_state(str(lst)) is None


if __name__ == "__main__":
    import inspect

    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        if inspect.signature(fn).parameters:  # fixture-based — pytest only
            print(f"skip (needs pytest): {name}")
            continue
        fn()
        print(f"ok: {name}")
    print("ALL PASS")
