"""One compose layout for every install, and production's data stays attached.

Until 2026-10-19 production ran `docker-compose.yml` plus an untracked
`docker-compose.serverports.yml`, while customers ran `docker-compose.customer.yml`,
a different file with different services, secrets and volumes. Every fix had
to land twice, and only the customer one was tested end to end. Now there is
one base and three overlays:

- `docker-compose.prod.yml`: prod's ports, worker concurrency and its existing
  data volumes;
- `docker-compose.dev.yml`: build from source, dev ports, the dev database;
- `docker-compose.reload.yml`: hot reload on top of dev.

Two things here would break production without any other test noticing:

1. A second layout growing back. A new top-level compose file is exactly how
   the drift started.
2. The prod overlay losing a data volume. Prod's volumes predate the layout and
   carry other names (`docker_db_data`, …). If `docker-compose.prod.yml` stops
   mapping one, compose quietly creates a fresh, empty `gdx_<key>` volume and
   prod boots on it. The data is not lost, only detached, which is worse
   because nothing errors.
"""

from __future__ import annotations

import re

import yaml

from gdx_dispatch.tools.tracked_files import REPO_ROOT, read_tracked_files

DOCKER = "gdx_dispatch/docker"
BASE = f"{DOCKER}/docker-compose.yml"
PROD = f"{DOCKER}/docker-compose.prod.yml"
DEV = f"{DOCKER}/docker-compose.dev.yml"

# Every compose file allowed outside demo/ (the demo stack is standalone and
# runs on its own project; folding it in is an open item of the layout work).
ALLOWED = {
    BASE,
    PROD,
    DEV,
    f"{DOCKER}/docker-compose.reload.yml",
    f"{DOCKER}/docker-compose.monitoring.yml",
}

# Prod's existing data volumes, read live off the prod box (2026-10-19): base
# volume key -> the name the data already lives under.
PROD_DATA = {
    "gdx_db_data": "docker_db_data",
    "gdx_uploads": "docker_gdx_uploads",
    "gdx_recordings": "gdx_gdx_recordings",
    "gdx_plugins": "gdx_gdx_plugins",
    "gdx_redis_data": "gdx_redis_data",
}

# Where each data volume must be mounted in the base, so the prod mapping lands
# on the container that reads it (service, target).
DATA_MOUNTS = {
    "gdx_db_data": ("db", "/var/lib/postgresql/data"),
    "gdx_uploads": ("app", "/app/uploads"),
    "gdx_recordings": ("app", "/app/recordings"),
    "gdx_plugins": ("plugin-host", "/plugins"),
    "gdx_redis_data": ("redis", "/data"),
}

_COMPOSE_NAME = re.compile(r"(?:^|/)docker-compose[\w.-]*\.ya?ml$")


def _load(rel: str) -> dict:
    return yaml.safe_load((REPO_ROOT / rel).read_text()) or {}


def _mounts(spec: dict, service: str) -> set[tuple[str, str]]:
    out = set()
    for mount in (spec["services"][service].get("volumes") or []):
        source, _, rest = str(mount).partition(":")
        out.add((source, rest.split(":", 1)[0]))
    return out


def test_no_second_layout_grows_back() -> None:
    found = {p for p in read_tracked_files() if _COMPOSE_NAME.search(p) and not p.startswith(f"{DOCKER}/demo/")}
    assert found == ALLOWED, (
        f"compose files changed: extra {sorted(found - ALLOWED)}, missing {sorted(ALLOWED - found)}. "
        "Customers and prod run docker-compose.yml; a difference belongs in an overlay."
    )


def test_base_mounts_every_data_volume_where_it_is_read() -> None:
    base = _load(BASE)
    for key, (service, target) in DATA_MOUNTS.items():
        assert key in (base.get("volumes") or {}), f"base declares no volume {key}"
        assert (key, target) in _mounts(base, service), f"base {service} does not mount {key} on {target}"


def test_prod_maps_exactly_its_existing_data_volumes() -> None:
    volumes = _load(PROD).get("volumes") or {}
    mapped = {key: cfg.get("name") for key, cfg in volumes.items() if (cfg or {}).get("external")}
    assert mapped == PROD_DATA, (
        "docker-compose.prod.yml must map each prod data volume to the name its data lives under, "
        "external so compose can neither create an empty one nor delete it; and nothing else, "
        "because secrets, backups and Caddy have no prod predecessor"
    )
    assert set(volumes) == set(PROD_DATA), f"prod overlay declares non-external volumes: {set(volumes) - set(PROD_DATA)}"


def test_prod_overlay_cannot_drop_base_settings() -> None:
    # Compose merges an overlay's maps into the base by key, so prod.yml can
    # add a variable but not remove one. `!reset` and `!override` are the two
    # tags that can; the old serverports.yml used both. A prod-only removal
    # belongs in the base, where the customer stack sees it too.
    text = (REPO_ROOT / PROD).read_text()
    assert not re.search(r"!(reset|override)\b", text)


def test_app_receives_the_existing_data_lock() -> None:
    # Prod's .env sets GDX_REQUIRE_EXISTING_DATA=1 so bootstrap_app refuses to
    # seed a fresh tenant into an empty database (the detached-volume case in
    # the docstring). The lock only works if compose hands it to the container
    # that runs bootstrap, and no overlay pins it to a literal.
    env = _load(BASE)["services"]["app"].get("environment") or {}
    assert env.get("GDX_REQUIRE_EXISTING_DATA") == "${GDX_REQUIRE_EXISTING_DATA:-}", (
        "base app must forward GDX_REQUIRE_EXISTING_DATA from .env"
    )
    for rel in (PROD, DEV):
        for service, spec in (_load(rel).get("services") or {}).items():
            overlay_env = (spec or {}).get("environment") or {}
            if isinstance(overlay_env, dict):
                assert "GDX_REQUIRE_EXISTING_DATA" not in overlay_env, f"{rel}: {service} overrides the lock"


def test_no_overlay_mounts_over_a_data_volume() -> None:
    # An overlay mount on the same target would shadow the data volume.
    targets = {(svc, target) for svc, target in DATA_MOUNTS.values()}
    for rel in (PROD, DEV):
        spec = _load(rel)
        for service in spec.get("services") or {}:
            for _, target in _mounts(spec, service):
                assert (service, target) not in targets, f"{rel}: {service} remounts {target}"


def test_caddy_never_starts_in_prod_or_dev() -> None:
    # Prod's host nginx and a dev box's own services hold 80/443; the profile
    # names are never enabled anywhere.
    for rel in (PROD, DEV):
        assert _load(rel)["services"]["caddy"].get("profiles"), f"{rel} does not disable caddy"
