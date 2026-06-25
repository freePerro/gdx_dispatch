"""Security-logic tests for the plugin browser stream (ADR-014).

Covers the allowlist + SSRF navigation guard — the parts that, if wrong, let the
server browser egress to an arbitrary/internal host. Loaded by path so it needs
no DB/app context.
"""
import importlib.util
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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("ALL PASS")
