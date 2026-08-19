"""The plugin storefront: read the curated catalog, fetch a wheel, verify it.

The catalog is published by the `gdx_dispatch_plugins` repository, which is the
curation authority — a plugin is listed if and only if it is merged and released
there. This module only reads it.

Why the CORE app does the fetching: plugin-host has no network egress in
production (that is the whole reason `pip install` cannot resolve anything at
boot), so it cannot download a wheel. The app can. It fetches the bytes,
verifies them against the catalog's digest, and hands them to the existing
artifact install path — from that row onward this is exactly the flow an
operator gets by uploading a wheel by hand.

Two things this module deliberately does NOT do:

* **It never takes a URL from the caller.** The client sends a plugin key and
  version; the download URL is resolved from the catalog entry and re-checked
  against a host allowlist. A client-supplied URL would make this an
  authenticated SSRF gadget pointed at the app's own network.
* **It never writes `plugin_registry`.** A store install is an artifact
  install. A registry row naming an index package would make plugin-host try to
  `pip install` it from PyPI on its next boot, which cannot resolve without
  egress — the plugin would go `degraded`, `/ready` would 503, and the
  container would flip unhealthy at exactly the moment the owner expects their
  new plugin to appear.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import time
import zipfile
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

DEFAULT_CATALOG_URL = "https://plugins.gdxdispatch.com/catalog.json"

#: Hosts a wheel may be downloaded from. The catalog names its own asset URLs,
#: but the catalog is remote data — pinning the hosts here means a compromised
#: or mistyped catalog still cannot point the app at an internal address.
_ALLOWED_WHEEL_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "plugins.gdxdispatch.com",
}

#: Same cap as a manual upload — a plugin wheel is small; a big one is a red
#: flag, not a plugin.
MAX_WHEEL_BYTES = 50 * 1024 * 1024

#: The catalog is small; a slow or huge response is a failure, not a wait.
_CATALOG_TIMEOUT_S = 10.0
_MAX_CATALOG_BYTES = 2 * 1024 * 1024
_WHEEL_TIMEOUT_S = 60.0
#: Redirect hops we will follow. GitHub release downloads take one or two.
_MAX_REDIRECTS = 5

#: Newer MAJOR schema means the catalog is describing things this build cannot
#: model; refuse rather than guess.
SUPPORTED_SCHEMA = 1

_CACHE_TTL_S = 900.0
_cache: dict = {"at": 0.0, "url": None, "catalog": None}


class StorefrontError(Exception):
    """A storefront operation failed in a way the owner should see verbatim."""


def catalog_url() -> str:
    return os.getenv("GDX_PLUGIN_CATALOG_URL", DEFAULT_CATALOG_URL).strip() or DEFAULT_CATALOG_URL


def reset_cache() -> None:
    _cache.update({"at": 0.0, "url": None, "catalog": None})


def _validate_entry(entry: dict) -> dict:
    """Keep only entries we can install and describe honestly."""
    if not isinstance(entry, dict):
        raise StorefrontError(f"catalog entry is not an object: {type(entry).__name__}")
    required = ("key", "distribution", "version", "name", "wheel_url", "sha256")
    if any(not isinstance(entry.get(f), str) or not entry.get(f) for f in required):
        raise StorefrontError(f"catalog entry missing required fields: {entry.get('key')!r}")
    if not isinstance(entry.get("permissions", []), list):
        raise StorefrontError(f"catalog entry {entry['key']!r} has a malformed permissions list")
    _check_wheel_url(entry["wheel_url"])
    return {
        "key": str(entry["key"]),
        "distribution": str(entry["distribution"]),
        "version": str(entry["version"]),
        "name": str(entry["name"]),
        "description": str(entry.get("description") or ""),
        "author": str(entry.get("author") or ""),
        "tier": str(entry.get("tier") or ""),
        "permissions": [str(p) for p in entry.get("permissions", [])],
        "requires": str(entry.get("requires") or ""),
        "license": str(entry.get("license") or "free"),
        "wheel_url": str(entry["wheel_url"]),
        "sha256": str(entry["sha256"]).lower(),
        # Display-only, and remote-controlled: a catalog saying "1e999" or {}
        # must not turn the Browse tab into a 500.
        "size": _safe_int(entry.get("size")),
    }


def _safe_int(value: object) -> int:
    try:
        n = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return n if 0 <= n <= MAX_WHEEL_BYTES else 0


def _check_wheel_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise StorefrontError(f"refusing a non-https wheel url: {url!r}")
    if (parsed.hostname or "").lower() not in _ALLOWED_WHEEL_HOSTS:
        raise StorefrontError(
            f"refusing a wheel url outside the allowed hosts: {parsed.hostname!r}"
        )


def parse_catalog(payload: dict) -> list[dict]:
    """Validate a catalog document and return its installable entries."""
    if not isinstance(payload, dict):
        raise StorefrontError("catalog is not a JSON object")
    schema = payload.get("schema_version")
    if not isinstance(schema, int):
        raise StorefrontError("catalog has no schema_version")
    if schema > SUPPORTED_SCHEMA:
        raise StorefrontError(
            f"this catalog uses schema {schema}, newer than this GDX build understands "
            f"({SUPPORTED_SCHEMA}). Update GDX to browse it."
        )
    plugins = payload.get("plugins")
    if not isinstance(plugins, list):
        raise StorefrontError("catalog has no plugins list")
    # v1 lists free plugins only. A paid entry is skipped rather than shown as
    # installable, since nothing here can complete a purchase.
    free = [e for e in plugins if isinstance(e, dict) and e.get("license", "free") == "free"]
    return [_validate_entry(e) for e in free]


def fetch_catalog(force: bool = False) -> list[dict]:
    """The curated catalog, cached briefly. Raises StorefrontError when it can't
    be read — the Browse tab says so rather than showing an empty store, which
    would read as "no plugins exist"."""
    url = catalog_url()
    now = time.monotonic()
    if (not force and _cache["catalog"] is not None and _cache["url"] == url
            and now - _cache["at"] < _CACHE_TTL_S):
        return _cache["catalog"]

    try:
        with (
            httpx.Client(timeout=_CATALOG_TIMEOUT_S, follow_redirects=True) as client,
            client.stream("GET", url, headers={"accept": "application/json"}) as r,
        ):
            if r.status_code >= 400:
                raise StorefrontError(f"the plugin catalog returned HTTP {r.status_code}")
            # Streamed with a running total: reading the whole body first and
            # then measuring it is not a cap, it is a report.
            chunks, total = [], 0
            for chunk in r.iter_bytes():
                total += len(chunk)
                if total > _MAX_CATALOG_BYTES:
                    raise StorefrontError("catalog response is implausibly large")
                chunks.append(chunk)
        payload = json.loads(b"".join(chunks))
    except StorefrontError:
        raise
    except Exception as exc:
        # The URL is operator-set and the response is remote; neither is echoed
        # into the owner's browser verbatim.
        log.warning("storefront catalog fetch failed url=%s err=%r", url, exc)
        raise StorefrontError("could not read the plugin catalog") from exc

    try:
        entries = parse_catalog(payload)
    except StorefrontError:
        raise
    except Exception as exc:
        # A malformed catalog is a catalog problem, not a server error — the
        # Browse tab must say so rather than 500.
        log.warning("storefront catalog is malformed: %r", exc)
        raise StorefrontError("the plugin catalog is malformed") from exc
    _cache.update({"at": now, "url": url, "catalog": entries})
    return entries


def find_entry(key: str, version: str | None = None) -> dict:
    for entry in fetch_catalog():
        if entry["key"] == key and (version is None or entry["version"] == version):
            return entry
    raise StorefrontError(
        f"{key!r}{'' if version is None else ' ' + version} is not in the plugin catalog"
    )


def download_wheel(entry: dict) -> tuple[str, bytes]:
    """Download and verify one catalog entry's wheel. Returns (filename, bytes).

    The URL comes from the catalog entry, never from a caller, and is re-checked
    against the host allowlist here as well — `_validate_entry` already did it,
    but this function is the one that actually opens a socket, so it does not
    rely on having been called correctly.
    """
    try:
        _final_url, content = _get_following_redirects(entry["wheel_url"])
    except StorefrontError:
        raise
    except Exception as exc:
        # Deliberately does NOT echo the upstream error text or URL: this string
        # is rendered in the owner's browser, and repeating an arbitrary
        # response back would turn a failed fetch into an information channel.
        log.warning("storefront download failed for %s: %r", entry["key"], exc)
        raise StorefrontError(f"could not download {entry['key']} from the catalog") from exc

    digest = hashlib.sha256(content).hexdigest()
    if digest != entry["sha256"]:
        raise StorefrontError(
            f"{entry['key']} failed its integrity check — the downloaded file does not "
            "match the digest the catalog published. Nothing was installed."
        )

    # The filename comes from the URL the CATALOG published, not from where the
    # download ended up: GitHub redirects release assets to a CDN path that is a
    # bare UUID, so the final URL has no usable filename at all. The catalog's
    # name is also the one the identity check below holds it to.
    filename = os.path.basename(urlparse(entry["wheel_url"]).path)
    _check_wheel_identity(entry, filename, content)
    return filename, content


def _get_following_redirects(url: str) -> tuple[str, bytes]:
    """Fetch `url`, following redirects MANUALLY so every hop is checked first.

    httpx's `follow_redirects=True` resolves the whole chain inside `send()`, so
    inspecting `response.history` afterwards is too late: the request to the
    redirect target has already been sent and answered. That is not a
    theoretical distinction — it is the difference between "we refuse to install
    from an internal address" and "we will happily probe one for you and report
    the status code", which is a port scanner with an oracle.

    Checking before each request is the only ordering that actually prevents the
    request.
    """
    with httpx.Client(timeout=_WHEEL_TIMEOUT_S, follow_redirects=False) as client:
        for _hop in range(_MAX_REDIRECTS):
            _check_wheel_url(url)  # BEFORE the socket is opened, every time
            with client.stream("GET", url) as r:
                if r.is_redirect:
                    location = r.headers.get("location")
                    if not location:
                        raise StorefrontError("catalog wheel redirected with no location")
                    url = str(httpx.URL(url).join(location))
                    continue
                if r.status_code >= 400:
                    raise StorefrontError(
                        f"the catalog's wheel host returned HTTP {r.status_code}"
                    )
                chunks, total = [], 0
                for chunk in r.iter_bytes():
                    total += len(chunk)
                    if total > MAX_WHEEL_BYTES:
                        raise StorefrontError("wheel is larger than the 50 MB cap")
                    chunks.append(chunk)
                return url, b"".join(chunks)
    raise StorefrontError("catalog wheel redirected too many times")


def _check_wheel_identity(entry: dict, filename: str, content: bytes) -> None:
    """The downloaded wheel must BE the plugin the card advertised.

    sha256 proves the bytes match what the catalog claimed; it says nothing
    about whether that claim matches the card. Without this, an entry titled
    "CHI Pricing 1.0.0" could carry a wheel for a different distribution
    entirely and the app would install it — and since desired-state keys off the
    stored filename, the card would then read "available" forever, because
    nothing it looks for ever appears.
    """
    from gdx_dispatch.plugin_host.reconcile import _canon, artifact_name_version

    want_dist, want_ver = _canon(entry["distribution"]), entry["version"]

    name_dist, name_ver = artifact_name_version(filename)
    if not name_dist or _canon(name_dist) != want_dist or name_ver != want_ver:
        raise StorefrontError(
            f"catalog entry {entry['key']!r} points at {filename!r}, which is not "
            f"{entry['distribution']} {entry['version']}. Nothing was installed."
        )

    # The wheel's own metadata, not just its filename — a filename is a label,
    # and this is the thing pip will actually believe.
    meta_name, meta_ver = _wheel_self_reported(content)
    if meta_name is None:
        raise StorefrontError(f"{filename!r} has no readable wheel metadata")
    if _canon(meta_name) != want_dist or meta_ver != want_ver:
        raise StorefrontError(
            f"catalog entry {entry['key']!r} advertises {entry['distribution']} "
            f"{entry['version']} but the wheel identifies itself as {meta_name} "
            f"{meta_ver}. Nothing was installed."
        )


def _wheel_self_reported(content: bytes) -> tuple[str | None, str | None]:
    """(Name, Version) from a wheel's own dist-info METADATA, or (None, None)."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            path = next((n for n in z.namelist() if n.endswith(".dist-info/METADATA")), None)
            if path is None:
                return None, None
            raw = z.read(path).decode("utf-8", "replace")
    except Exception:
        return None, None
    name = version = None
    for line in raw.splitlines():
        if not line.strip():
            break  # headers end at the first blank line
        if line.lower().startswith("name:"):
            name = line.split(":", 1)[1].strip()
        elif line.lower().startswith("version:"):
            version = line.split(":", 1)[1].strip()
    return name, version


def _is_newer(candidate: str, current: str) -> bool:
    """True if `candidate` is a strictly newer version than `current`."""
    try:
        from packaging.version import InvalidVersion, Version

        try:
            return Version(candidate) > Version(current)
        except InvalidVersion:
            return candidate != current
    except ImportError:  # pragma: no cover - packaging ships with pip
        return candidate != current


def merge_install_state(entries: list[dict], installed: dict[str, str],
                        running: dict[str, str]) -> list[dict]:
    """Annotate catalog entries with what this instance actually has.

    `installed` — {canonical distribution: version} the operator has asked for.
    `running`   — {canonical distribution: version} plugin-host reports LOADED.

    Both are reported because they can legitimately disagree: an install is
    recorded immediately but only takes effect on the next plugin-host restart.
    Collapsing them into one "installed" flag is what would let the UI claim a
    plugin is running before it is.
    """
    from gdx_dispatch.plugin_host.reconcile import _canon, _versions_equal

    out = []
    for entry in entries:
        canon = _canon(entry["distribution"])
        have, live = installed.get(canon), running.get(canon)
        item = dict(entry)
        item["installed_version"] = have
        item["running_version"] = live
        # Only a NEWER catalog version is an update. A plain "differs" check
        # labels a rollback — an instance deliberately pinned ahead of, or
        # behind, the catalog — as "Update to 1.0.0", which is a downgrade
        # wearing an upgrade's button.
        item["update_available"] = any(
            _is_newer(entry["version"], v) for v in (have, live) if v
        )
        if live and _versions_equal(live, entry["version"]):
            item["state"] = "running"
        elif have:
            item["state"] = "pending_restart"
        else:
            item["state"] = "available"
        out.append(item)
    return out
