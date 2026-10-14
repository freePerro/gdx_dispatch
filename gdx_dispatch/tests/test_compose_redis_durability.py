"""Every tracked compose file's redis keeps the auth state: noeviction, AOF, a /data volume.

Redis is the Celery broker and also the refresh-token store: `used_refresh_jtis`
(replay detection) and `refresh_family:{sub}` (family revoke, invariant 7 /
RFC 9700). All three compose files ran `--maxmemory-policy allkeys-lru` with no
`--appendonly` and no volume of their own on /data (GDXA-1117). The image's
anonymous /data volume kept the RDB snapshot (up to an hour stale) across a
restart and an `up -d` recreate, but not across `down`; and a full cache could
evict the one replay set whole.

Every security key is written with an `expire`, so a `volatile-*` policy evicts
them just as readily; only `noeviction` keeps them. A guard on the word "lru"
would therefore pass a wrong fix, which is why the policy is pinned by value.

The guard reads every tracked `docker-compose*.yml` that defines a `redis`
service, so a fourth compose file that grows a redis is covered without an edit.
"""

from __future__ import annotations

import re

import pytest
import yaml

from gdx_dispatch.tools.tracked_files import REPO_ROOT, read_tracked_files

_COMPOSE_NAME = re.compile(r"(?:^|/)docker-compose[\w.-]*\.ya?ml$")
_POLICY = re.compile(r"--maxmemory-policy\s+(\S+?)'?(?:\s|$)")
_APPENDONLY = re.compile(r"--appendonly\s+(\w+)")

# The files the GDXA-1117 fix touched. Pinned so a rename that drops one from
# the scan, or a yaml the loader stops reading, is red rather than vacuous.
EXPECTED = {
    "gdx_dispatch/docker/docker-compose.yml",
    "gdx_dispatch/docker/demo/docker-compose.demo.yml",
    "gdx_dispatch/docker/docker-compose.customer.yml",
}


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader plus Compose's merge tags, which the overlays use (`!override`).

    `!reset` reads as null and `!override` as its plain value, so an overlay that
    redefines redis is checked on what it sets.
    """


def _plain(loader: yaml.SafeLoader, node: yaml.Node) -> object:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)  # type: ignore[arg-type]


_ComposeLoader.add_constructor("!override", _plain)
_ComposeLoader.add_constructor("!reset", lambda loader, node: None)


def _redis_services() -> dict[str, tuple[dict, dict]]:
    """path -> (redis service spec, top-level volumes) for each compose with a redis."""
    found: dict[str, tuple[dict, dict]] = {}
    for rel in sorted(p for p in read_tracked_files() if _COMPOSE_NAME.search(p)):
        spec = yaml.load((REPO_ROOT / rel).read_text(), Loader=_ComposeLoader) or {}  # noqa: S506 — SafeLoader subclass
        redis = (spec.get("services") or {}).get("redis")
        if redis is not None:
            found[rel] = (redis, spec.get("volumes") or {})
    return found


_SERVICES = _redis_services()


def _command(redis: dict) -> str:
    cmd = redis.get("command") or ""
    return " ".join(cmd) if isinstance(cmd, list) else str(cmd)


def test_scan_finds_every_compose_redis() -> None:
    assert set(_SERVICES) >= EXPECTED, f"redis compose files missing from the scan: {EXPECTED - set(_SERVICES)}"


@pytest.mark.parametrize("rel", sorted(_SERVICES))
def test_redis_policy_is_noeviction(rel: str) -> None:
    policies = _POLICY.findall(_command(_SERVICES[rel][0]))
    assert policies == ["noeviction"], (
        f"{rel}: redis --maxmemory-policy is {policies or 'unset'}; the refresh-token "
        "keys all carry a TTL, so anything but noeviction can evict them (GDXA-1117)"
    )


@pytest.mark.parametrize("rel", sorted(_SERVICES))
def test_redis_appendonly_is_on(rel: str) -> None:
    # Redis applies the last occurrence, so `--appendonly yes --appendonly no` is off.
    values = _APPENDONLY.findall(_command(_SERVICES[rel][0]))
    assert values[-1:] == ["yes"], f"{rel}: redis runs with --appendonly {values or 'unset'}, not yes"


@pytest.mark.parametrize("rel", sorted(_SERVICES))
def test_redis_data_is_a_named_volume(rel: str) -> None:
    redis, top_volumes = _SERVICES[rel]
    named = []
    for mount in redis.get("volumes") or []:
        source, _, rest = str(mount).partition(":")
        target = rest.split(":", 1)[0]
        if target == "/data":
            named.append(source)
    assert len(named) == 1, f"{rel}: redis mounts no volume on /data, so the AOF dies with the container"
    assert named[0] in top_volumes, (
        f"{rel}: redis /data source {named[0]!r} is not a declared named volume (a bind path or typo)"
    )
