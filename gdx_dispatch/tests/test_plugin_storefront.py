"""The plugin storefront: catalog reading, wheel fetching, install-state merge.

This is the one surface where the app downloads something from the internet and
then arranges for it to be executed, so most of these tests are about what it
REFUSES: a client-supplied URL, a non-https URL, a host outside the allowlist, a
redirect that leaves the allowlist, an oversized body, a digest mismatch, or a
catalog from a future schema.

Runs in the docker image (imports core).
"""
from __future__ import annotations

import hashlib

import pytest

from gdx_dispatch.core import plugin_storefront as store

ASSET = "https://github.com/freePerro/gdx_dispatch_plugins/releases/download/v0.1.0"
MAX = store.MAX_WHEEL_BYTES


def _make_wheel(dist: str, version: str) -> bytes:
    """A real zip carrying dist-info METADATA — the identity check reads it."""
    import io
    import zipfile

    info = f"{dist.replace('-', '_')}-{version}.dist-info"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{info}/METADATA",
                   f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n\nbody\n")
        z.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n")
    return buf.getvalue()


#: The wheel the default catalog entry advertises.
WHEEL = _make_wheel("gdx-plugin-n8n", "0.1.0")
DIGEST = hashlib.sha256(WHEEL).hexdigest()


def _entry(**over) -> dict:
    e = {
        "key": "n8n",
        "distribution": "gdx-plugin-n8n",
        "version": "0.1.0",
        "name": "n8n Automations",
        "description": "Automations console.",
        "author": "GDX Dispatch",
        "tier": "starter",
        "permissions": ["events"],
        "requires": "",
        "license": "free",
        "wheel_url": f"{ASSET}/gdx_plugin_n8n-0.1.0-py3-none-any.whl",
        "sha256": DIGEST,
        "size": len(WHEEL),
    }
    e.update(over)
    return e


def _catalog(*entries, schema=1) -> dict:
    return {"schema_version": schema, "plugins": list(entries or [_entry()])}


@pytest.fixture(autouse=True)
def _clear_cache():
    store.reset_cache()
    yield
    store.reset_cache()


# ── catalog parsing ──────────────────────────────────────────────────────────

def test_a_valid_catalog_parses_and_keeps_the_permission_list():
    entries = store.parse_catalog(_catalog())
    assert len(entries) == 1
    assert entries[0]["key"] == "n8n"
    # The owner is shown these BEFORE installing — losing them silently would
    # defeat the point of the pre-install permission display.
    assert entries[0]["permissions"] == ["events"]


def test_a_newer_schema_is_refused_rather_than_guessed():
    with pytest.raises(store.StorefrontError, match="newer than this GDX build"):
        store.parse_catalog(_catalog(schema=99))


def test_a_catalog_without_a_schema_is_refused():
    with pytest.raises(store.StorefrontError, match="schema_version"):
        store.parse_catalog({"plugins": []})


def test_an_entry_missing_required_fields_is_refused():
    with pytest.raises(store.StorefrontError, match="missing required fields"):
        store.parse_catalog(_catalog(_entry(sha256="")))


def test_paid_entries_are_skipped_not_shown_as_installable():
    """v1 is free-only; nothing here can complete a purchase."""
    entries = store.parse_catalog(_catalog(_entry(), _entry(key="paid", license="paid")))
    assert [e["key"] for e in entries] == ["n8n"]


@pytest.mark.parametrize("size", ["1e999", 1e400, {}, [], "abc", -5, None, 10**400])
def test_a_hostile_size_field_cannot_break_browsing(size):
    """`size` is display-only and remote-controlled. A catalog that puts an
    unrepresentable number there must not turn the Browse tab into a 500."""
    entries = store.parse_catalog(_catalog(_entry(size=size)))
    assert isinstance(entries[0]["size"], int)
    assert 0 <= entries[0]["size"] <= store.MAX_WHEEL_BYTES


@pytest.mark.parametrize("plugins", [[None], ["a string"], [123], [[]]])
def test_non_object_entries_do_not_crash_the_parser(plugins):
    """A StorefrontError is a catalog problem the owner can be told about; an
    AttributeError is a 500."""
    result = store.parse_catalog({"schema_version": 1, "plugins": plugins})
    assert result == []


def test_a_non_string_required_field_is_refused_not_coerced():
    with pytest.raises(store.StorefrontError, match="missing required fields"):
        store.parse_catalog(_catalog(_entry(version={"$gt": 0})))


# ── URL safety ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "http://github.com/x/y.whl",                      # not https
    "https://169.254.169.254/latest/meta-data/",      # cloud metadata
    "https://plugin-host:8000/internal/restart",      # an internal service
    "https://localhost/x.whl",
    "https://evil.example.com/x.whl",
    "file:///etc/passwd",
])
def test_a_wheel_url_outside_the_allowlist_is_refused(url):
    with pytest.raises(store.StorefrontError):
        store.parse_catalog(_catalog(_entry(wheel_url=url)))


def test_the_allowed_release_host_is_accepted():
    assert store.parse_catalog(_catalog())[0]["wheel_url"].startswith("https://github.com/")


# ── downloading ──────────────────────────────────────────────────────────────

def _patch_stream(monkeypatch, handler):
    """Run the REAL httpx client against a mock transport.

    Deliberately not a hand-rolled fake response: the bug this file exists to
    prevent lived in httpx's own redirect handling, and a fake with a
    hand-built `.history` cannot reproduce it. `requested` records every URL
    that actually reaches the transport — i.e. every request that would have
    hit the network.
    """
    requested: list[str] = []

    def _record(request):
        requested.append(str(request.url))
        return handler(request)

    transport = __import__("httpx").MockTransport(_record)
    real_client = store.httpx.Client
    monkeypatch.setattr(
        store.httpx, "Client",
        lambda **kw: real_client(**{**kw, "transport": transport}),
    )
    return requested


def _ok(content: bytes):
    return lambda request: __import__("httpx").Response(200, content=content)


def test_a_verified_wheel_downloads(monkeypatch):
    entry = _entry()
    _patch_stream(monkeypatch, _ok(WHEEL))
    name, content = store.download_wheel(entry)
    assert name == "gdx_plugin_n8n-0.1.0-py3-none-any.whl"
    assert content == WHEEL


def test_a_digest_mismatch_refuses_to_install(monkeypatch):
    """The catalog's digest is the only thing standing between a tampered
    download and code execution on the plugin-host."""
    entry = _entry(sha256="0" * 64)
    _patch_stream(monkeypatch, _ok(WHEEL))
    with pytest.raises(store.StorefrontError, match="integrity check"):
        store.download_wheel(entry)


def test_an_oversized_wheel_is_cut_off(monkeypatch):
    entry = _entry()
    _patch_stream(monkeypatch, _ok(b"x" * (MAX + 1)))
    with pytest.raises(store.StorefrontError, match="50 MB cap"):
        store.download_wheel(entry)


def test_a_redirect_to_an_internal_address_is_never_REQUESTED(monkeypatch):
    """The one that matters: refusing the bytes is not the same as refusing the
    request.

    httpx's own `follow_redirects=True` resolves the whole chain inside
    `send()`, so a check on `response.history` runs after the internal request
    has already been sent and answered — an authenticated port scanner with a
    status-code oracle, wearing an allowlist. This asserts on what actually
    reached the transport, so it fails if that ordering ever returns.
    """
    internal = "https://169.254.169.254/latest/meta-data/"

    def _redirect_inward(request):
        import httpx as _h
        if "169.254" in str(request.url):
            return _h.Response(200, content=b"SECRET")
        return _h.Response(302, headers={"location": internal})

    requested = _patch_stream(monkeypatch, _redirect_inward)

    with pytest.raises(store.StorefrontError, match="outside the allowed hosts"):
        store.download_wheel(_entry())

    assert not any("169.254" in u for u in requested), (
        f"the internal address was actually requested: {requested}"
    )


def test_the_filename_comes_from_the_catalog_not_the_redirect_target(monkeypatch):
    """GitHub redirects release assets to a CDN path that is a bare UUID.

    Taking the filename from where the download ENDED would store the artifact
    under a name reconcile cannot parse — so it would never install, and the
    card would read "available" forever. Caught against the real CDN, not by a
    test: every mock in this file used a tidy final URL.
    """
    opaque = "https://objects.githubusercontent.com/f5ef7f57-1b9a-4d7b-ab75-bc68d20dacd2"

    def _redirect_to_uuid(request):
        import httpx as _h
        if "objects.githubusercontent.com" in str(request.url):
            return _h.Response(200, content=WHEEL)
        return _h.Response(302, headers={"location": opaque})

    _patch_stream(monkeypatch, _redirect_to_uuid)
    name, content = store.download_wheel(_entry())

    assert name == "gdx_plugin_n8n-0.1.0-py3-none-any.whl"
    assert content == WHEEL


def test_a_redirect_within_the_allowlist_is_followed(monkeypatch):
    """Real release downloads redirect to the asset CDN, so this must work."""
    final = ("https://objects.githubusercontent.com/"
             "gdx_plugin_n8n-0.1.0-py3-none-any.whl")

    def _redirect_to_cdn(request):
        import httpx as _h
        if "objects.githubusercontent.com" in str(request.url):
            return _h.Response(200, content=WHEEL)
        return _h.Response(302, headers={"location": final})

    requested = _patch_stream(monkeypatch, _redirect_to_cdn)
    name, content = store.download_wheel(_entry())

    assert content == WHEEL
    assert len(requested) == 2 and requested[-1] == final


def test_a_redirect_loop_terminates(monkeypatch):
    def _loop(request):
        import httpx as _h
        return _h.Response(302, headers={"location": str(request.url)})

    _patch_stream(monkeypatch, _loop)
    with pytest.raises(store.StorefrontError, match="too many times"):
        store.download_wheel(_entry())


def test_a_download_failure_is_reported_without_echoing_the_upstream(monkeypatch):
    """The failure text is rendered in the owner's browser, so it must not
    repeat an arbitrary upstream response back to them."""
    import httpx as _h
    _patch_stream(monkeypatch, lambda r: _h.Response(403, content=b"internal secret body"))

    with pytest.raises(store.StorefrontError) as e:
        store.download_wheel(_entry())

    assert "403" in str(e.value)
    assert "internal secret body" not in str(e.value)


# ── the wheel must BE the plugin the card advertised ─────────────────────────

def test_a_wheel_for_a_different_distribution_is_refused(monkeypatch):
    """sha256 pins the bytes to the catalog's claim — not to the card's identity.

    Without this check a card titled "n8n Automations 0.1.0" could ship a wheel
    for something else entirely, and the app would install it.
    """
    other = _make_wheel("gdx-plugin-other", "9.9.9")
    entry = _entry(
        wheel_url=f"{ASSET}/gdx_plugin_other-9.9.9-py3-none-any.whl",
        sha256=hashlib.sha256(other).hexdigest(),
    )
    _patch_stream(monkeypatch, _ok(other))

    with pytest.raises(store.StorefrontError, match="which is not gdx-plugin-n8n"):
        store.download_wheel(entry)


def test_a_wheel_whose_metadata_disagrees_with_its_filename_is_refused(monkeypatch):
    """A filename is a label; the metadata is what pip believes."""
    mislabelled = _make_wheel("gdx-plugin-other", "9.9.9")
    entry = _entry(
        wheel_url=f"{ASSET}/gdx_plugin_n8n-0.1.0-py3-none-any.whl",
        sha256=hashlib.sha256(mislabelled).hexdigest(),
    )
    _patch_stream(monkeypatch, _ok(mislabelled))

    with pytest.raises(store.StorefrontError, match="identifies itself as"):
        store.download_wheel(entry)


def test_a_matching_wheel_passes_the_identity_check(monkeypatch):
    good = _make_wheel("gdx-plugin-n8n", "0.1.0")
    entry = _entry(sha256=hashlib.sha256(good).hexdigest())
    _patch_stream(monkeypatch, _ok(good))
    name, content = store.download_wheel(entry)
    assert content == good


# ── install-state merge ──────────────────────────────────────────────────────

def test_a_plugin_nobody_installed_reads_as_available():
    [item] = store.merge_install_state([_entry()], {}, {})
    assert item["state"] == "available"
    assert item["installed_version"] is None and item["running_version"] is None
    assert item["update_available"] is False


def test_recorded_but_not_yet_loaded_reads_as_pending_restart():
    """The honest state: an install is recorded immediately but only takes
    effect on the next plugin-host restart. Claiming "running" here is exactly
    the fake-success this codebase treats as a top-class defect."""
    [item] = store.merge_install_state([_entry()], {"gdx_plugin_n8n": "0.1.0"}, {})
    assert item["state"] == "pending_restart"


def test_loaded_at_the_catalog_version_reads_as_running():
    [item] = store.merge_install_state(
        [_entry()], {"gdx_plugin_n8n": "0.1.0"}, {"gdx_plugin_n8n": "0.1.0"})
    assert item["state"] == "running"
    assert item["update_available"] is False


def test_an_older_running_version_offers_an_update():
    [item] = store.merge_install_state(
        [_entry(version="0.2.0")], {"gdx_plugin_n8n": "0.1.0"}, {"gdx_plugin_n8n": "0.1.0"})
    assert item["update_available"] is True


def test_a_newer_installed_version_is_not_offered_as_an_update():
    """An instance ahead of the catalog is not due an "update" — labelling a
    downgrade as an upgrade is how someone rolls themselves backwards."""
    [item] = store.merge_install_state(
        [_entry(version="0.1.0")], {"gdx_plugin_n8n": "0.9.0"}, {"gdx_plugin_n8n": "0.9.0"})
    assert item["update_available"] is False


def test_double_digit_minors_compare_as_versions_not_strings():
    """0.10.0 is newer than 0.9.0 — the comparison that bit reconcile too."""
    [item] = store.merge_install_state(
        [_entry(version="0.10.0")], {"gdx_plugin_n8n": "0.9.0"}, {})
    assert item["update_available"] is True


def test_distribution_naming_differences_still_match():
    """The catalog says `gdx-plugin-n8n`; the volume says `gdx_plugin_n8n`.
    Matching on the canonical form is what makes the join work at all."""
    [item] = store.merge_install_state([_entry()], {"gdx_plugin_n8n": "0.1.0"}, {})
    assert item["installed_version"] == "0.1.0"


def test_running_and_installed_are_reported_separately():
    """They can legitimately disagree, and collapsing them into one flag is how
    a UI ends up claiming a plugin is live before it is."""
    [item] = store.merge_install_state(
        [_entry(version="0.2.0")], {"gdx_plugin_n8n": "0.2.0"}, {"gdx_plugin_n8n": "0.1.0"})
    assert item["installed_version"] == "0.2.0"
    assert item["running_version"] == "0.1.0"
    assert item["state"] == "pending_restart"
