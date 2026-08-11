"""Per-plugin authorization (ADR-013).

Who may use which installed plugin. Two layers, resolved with OR:

  plugins.read / plugins.write        blanket — any installed plugin
  plugin.<key>.read / .write          this plugin only

The blanket keys are static (``core/permissions.py``) so they land in the
builtin admin contract; the per-plugin keys are generated from the installed
catalog, because which plugins exist is a deployment fact, not a code fact.

Everything here is pure except :func:`installed_plugin_keys`, so the rules can
be unit-tested without a plugin-host.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Final

from gdx_dispatch.core.permissions import WILDCARD

log = logging.getLogger(__name__)

# Plugin keys are lowercase module keys (PluginManifest.key). Bounded length so
# a hostile key can't be used to bloat the permission catalog.
_PLUGIN_KEY = r"[a-z0-9][a-z0-9_-]{0,63}"

PLUGIN_PERMISSION_RE: Final = re.compile(rf"^plugin\.{_PLUGIN_KEY}\.(read|write)$")
PLUGIN_KEY_RE: Final = re.compile(rf"^{_PLUGIN_KEY}$")

BLANKET_READ: Final = "plugins.read"
BLANKET_WRITE: Final = "plugins.write"

READ: Final = "read"
WRITE: Final = "write"

# Only GET/HEAD/OPTIONS are reads. Anything else — including a POST used as a
# search — counts as write. Deliberately strict: a new plugin route that
# mutates via POST is the common case, and the failure mode of being strict is
# a 403 someone reports, while the failure mode of being lax is silent writes.
_READ_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})


def is_plugin_permission(key: str) -> bool:
    """True for a well-formed per-plugin key, whether or not that plugin exists.

    Shape, not membership, on purpose. The role editor validates against this so
    a tenant's existing per-plugin grants survive a role save taken while
    plugin-host is unreachable — membership-based validation would silently drop
    every plugin grant during an outage.
    """
    return bool(PLUGIN_PERMISSION_RE.match(key or ""))


def permission_key(plugin_key: str, action: str) -> str:
    return f"plugin.{plugin_key}.{action}"


def blanket_key(action: str) -> str:
    return BLANKET_WRITE if action == WRITE else BLANKET_READ


def action_for_method(method: str) -> str:
    return READ if (method or "").upper() in _READ_METHODS else WRITE


def may_use_plugin(perms: set[str], plugin_key: str, action: str) -> bool:
    """Does this permission set allow `action` on `plugin_key`?

    OR across the two layers (``require_permission`` only does AND, which is why
    callers resolve the set themselves and ask here).
    """
    if not plugin_key:
        # No key means the caller could not identify the target plugin. Never
        # guess — an unidentifiable target is a denied one.
        return False
    if WILDCARD in perms:
        return True
    return blanket_key(action) in perms or permission_key(plugin_key, action) in perms


def may_grant_plugin_permission(grantor_perms: set[str], requested: str) -> bool:
    """May a grantor holding `grantor_perms` confer the per-plugin key `requested`?

    Without this, the delegation cap compares exact keys and an admin — whose
    contract is the blanket ``plugins.write``, never ``plugin.chipricing.write``
    — could see every per-plugin checkbox and tick none of them. Holding the
    blanket grant for an action is what confers the right to delegate that
    action on any single plugin.
    """
    if WILDCARD in grantor_perms:
        return True
    m = PLUGIN_PERMISSION_RE.match(requested or "")
    if not m:
        return False
    return blanket_key(m.group(1)) in grantor_perms


def catalog_entries(plugin_names: dict[str, str]) -> list[dict[str, str]]:
    """Per-plugin catalog rows for the Roles & Permissions grid.

    `plugin_names` maps plugin key → human label. Category is ``plugins``, which
    the frontend groups on with no code change (RolePermissionsView buckets
    whatever categories the catalog returns).
    """
    rows: list[dict[str, str]] = []
    for key in sorted(plugin_names):
        if not PLUGIN_KEY_RE.match(key):
            continue
        label = plugin_names[key] or key
        rows.append({"key": permission_key(key, READ), "label": f"Use {label}", "category": "plugins"})
        rows.append({
            "key": permission_key(key, WRITE),
            "label": f"Change data in {label}",
            "category": "plugins",
        })
    return rows


# Short-lived cache for the installed-plugin lookup below. Not an optimization:
# the Roles & Permissions screen calls this on a `get_current_user`-only
# endpoint, so without it ANY authenticated user could make the app issue a
# blocking upstream request per page load. plugin-host is the least reliable
# process in the stack (it pip-installs on boot and has been observed hanging),
# and sync endpoint bodies run in a bounded anyio threadpool — enough concurrent
# calls against a hung host would tie up every worker. Cache the failure too, so
# an outage costs one slow call per TTL rather than one per request.
_CATALOG_TTL_SECONDS = 60.0
_catalog_cache: tuple[float, dict[str, str]] | None = None
_catalog_lock = threading.Lock()


def _now() -> float:
    return time.monotonic()


def installed_plugin_keys(timeout: float = 1.5, *, refresh: bool = False) -> dict[str, str]:
    """{key: name} for installed plugins, or {} if plugin-host can't be reached.

    Best-effort by design: the permission CATALOG degrades to the blanket keys
    when plugin-host is down, but already-granted per-plugin keys are unaffected
    because validation is shape-based (see :func:`is_plugin_permission`).

    Note a plugin whose manifest key isn't lowercase gets no per-plugin rows
    (PluginManifest documents keys as lowercase); the blanket grant still
    reaches it, and the mismatch is logged rather than passing silently.
    """
    global _catalog_cache

    cached = _catalog_cache
    if not refresh and cached is not None and (_now() - cached[0]) < _CATALOG_TTL_SECONDS:
        return dict(cached[1])

    with _catalog_lock:
        # Re-check: another thread may have refreshed while we waited.
        cached = _catalog_cache
        if not refresh and cached is not None and (_now() - cached[0]) < _CATALOG_TTL_SECONDS:
            return dict(cached[1])
        out = _fetch_plugin_keys(timeout)
        _catalog_cache = (_now(), out)
        return dict(out)


def _fetch_plugin_keys(timeout: float) -> dict[str, str]:
    import httpx

    from gdx_dispatch.routers.plugins_proxy import _plugin_host_url

    try:
        resp = httpx.get(f"{_plugin_host_url()}/api/plugins", timeout=timeout)
        resp.raise_for_status()
        plugins = resp.json()
    except Exception:  # noqa: BLE001 — any failure means "no catalog", never a 500
        log.info("plugin_permission_catalog_unavailable", exc_info=True)
        return {}
    if not isinstance(plugins, list):
        return {}
    out: dict[str, str] = {}
    for p in plugins:
        if isinstance(p, dict) and isinstance(p.get("key"), str):
            key = p["key"]
            if not PLUGIN_KEY_RE.match(key):
                log.warning("plugin_key_not_grantable key=%r", key)
                continue
            out[key] = str(p.get("name") or key)
    return out


def reset_catalog_cache() -> None:
    """Drop the cached catalog — for tests and for the install/remove flow."""
    global _catalog_cache
    _catalog_cache = None
