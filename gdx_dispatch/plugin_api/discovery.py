"""Plugin discovery — finds installed third-party modules and gates them.

The "import" is `entry_points(group="gdx.modules").load()`: pip registers a
plugin there, this finds it. Discovery runs in the plugin-host container in two
phases (mount + migration), so the logic lives here, shared.

Stdlib only — `importlib.metadata` + `re`. Keep it that way so it stays
host-testable without the docker image.

See gdx_dispatch/docs/decisions/ADR-013-third-party-module-plugins.md.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Iterable

from gdx_dispatch.plugin_api.manifest import PluginManifest

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "gdx.modules"


def _ver_tuple(v: str) -> tuple[int, ...]:
    # Digits-only compare: "1.10.0-rc1" -> (1, 10, 0). Enough to answer
    # "is the host new enough"; swap for packaging.version if pre-release
    # ordering ever matters. (Same convention as admin_ops._ver_tuple.)
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:3])


def is_compatible(requires: str, current: str) -> bool:
    """Does host version `current` satisfy a plugin's `requires` constraint?

    v0 grammar: "" (any) or "gdx>=X.Y[.Z]". Anything else is **fail-closed** —
    an unparseable constraint must not silently load a plugin built against a
    contract we can't verify.
    """
    requires = (requires or "").strip()
    if not requires:
        return True
    m = re.match(r"^gdx\s*>=\s*([0-9][0-9.]*)$", requires)
    if not m:
        log.warning("plugin requires constraint not understood, failing closed: %r", requires)
        return False
    return _ver_tuple(current) >= _ver_tuple(m.group(1))


def load_manifests(entry_points: Iterable, current_version: str) -> list[PluginManifest]:
    """Resolve an iterable of entry points to validated, compatible manifests.

    Pure and injectable so it unit-tests with fake entry points (any object with
    `.name` and `.load()`). A plugin that errors on load, returns the wrong type,
    or fails the compat gate is **skipped with a log line**, never fatal — one
    bad plugin must not take down the whole plugin-host.
    """
    out: list[PluginManifest] = []
    for ep in entry_points:
        name = getattr(ep, "name", "<unknown>")
        try:
            manifest = ep.load()
        except Exception:
            log.exception("plugin %s failed to load — skipping", name)
            continue
        if not isinstance(manifest, PluginManifest):
            log.error("plugin %s did not export a PluginManifest (got %s) — skipping",
                      name, type(manifest).__name__)
            continue
        if not is_compatible(manifest.requires, current_version):
            log.warning("plugin %s skipped: requires %r, host is %s",
                        name, manifest.requires, current_version)
            continue
        out.append(manifest)
    return out


def discover_plugins(current_version: str | None = None) -> list[PluginManifest]:
    """Find all installed, compatible plugins via the gdx.modules entry-point group."""
    from importlib.metadata import entry_points

    cv: str = current_version or os.getenv("APP_VERSION", "0")
    eps = entry_points(group=ENTRY_POINT_GROUP)
    return load_manifests(eps, cv)


def discover_with_dists(
    current_version: str | None = None,
) -> list[tuple[PluginManifest, str | None, str | None]]:
    """Like discover_plugins, but each manifest is paired with the distribution
    that provides it: `(manifest, dist_name, dist_version)`. The installed version
    lets the host detect a STALE plugin (loaded version != operator's desired
    version) and fail closed. Each entry point is validated through load_manifests
    so the same skip-on-bad/compat rules apply."""
    from importlib.metadata import entry_points

    cv: str = current_version or os.getenv("APP_VERSION", "0")
    out: list[tuple[PluginManifest, str | None, str | None]] = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        manifests = load_manifests([ep], cv)
        if not manifests:
            continue
        dist = getattr(ep, "dist", None)
        out.append((manifests[0], getattr(dist, "name", None), getattr(dist, "version", None)))
    return out
