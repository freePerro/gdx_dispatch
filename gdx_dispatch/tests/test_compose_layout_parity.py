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

# Paths each third-party image declares as VOLUME (`docker image inspect`,
# 2026-10-20). A service on one of these images that mounts nothing there gets
# an anonymous volume, which then sits beside prod's named data volumes.
IMAGE_VOLUMES = {
    "postgres:16-alpine": {"/var/lib/postgresql/data"},
    "redis:7-alpine": {"/data"},
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


def _unmounted_image_volumes(spec: dict, service: str) -> set[str]:
    svc = spec["services"][service]
    tmpfs = svc.get("tmpfs") or []
    # tmpfs takes a string or a list, and an entry may carry options (`/x:size=1m`).
    tmpfs = [tmpfs] if isinstance(tmpfs, str) else tmpfs
    covered = {target for _, target in _mounts(spec, service)} | {str(t).split(":", 1)[0] for t in tmpfs}
    return IMAGE_VOLUMES.get(svc.get("image", ""), set()) - covered


def test_no_base_service_gets_an_anonymous_volume() -> None:
    base = _load(BASE)
    assert any(svc.get("image") in IMAGE_VOLUMES for svc in base["services"].values()), "table matches no service"
    for service in base["services"]:
        assert not _unmounted_image_volumes(base, service), (
            f"base {service} mounts nothing on {sorted(_unmounted_image_volumes(base, service))}, which its image "
            "declares as VOLUME: mount a named volume there, or a tmpfs if nothing is kept"
        )


def test_the_anonymous_volume_check_catches_the_old_backup_sidecar() -> None:
    """Falsifier: the backup service as it stood before 2026-10-20, with no tmpfs."""
    base = _load(BASE)
    base["services"]["backup"].pop("tmpfs")
    assert _unmounted_image_volumes(base, "backup") == {"/var/lib/postgresql/data"}


def test_prod_app_stays_reachable_by_the_nginx_container() -> None:
    # Prod's TLS front is the gdx-nginx container, configured outside this repo
    # (/var/www/gdx/infra), and it proxies to http://gdx-app-1:8000 by Docker
    # DNS on gdx_default, the old layout's default network. The base puts app on
    # backend/edge/automation only, so without this the switch answers 502 on
    # every domain (found by /audit, 2026-10-20). External: nginx is attached
    # to it, so compose must not try to create, recreate or delete it.
    prod = _load(PROD)
    nets = (prod["services"]["app"].get("networks") or {})
    attached = [key for key in nets if (prod.get("networks") or {}).get(key, {}).get("name") == "gdx_default"]
    assert attached, "prod app is not on gdx_default, where the gdx-nginx container resolves gdx-app-1"
    assert prod["networks"][attached[0]].get("external") is True
    # nginx names the container, gdx-app-1: project `gdx` and no container_name
    # on app. The overlay's `name:` covers a bare `docker compose` (which would
    # otherwise be project `docker`), update.sh's -p covers the script. Any of
    # these changing passes the check above and still 502s. The scan reads
    # text only: a -p split across lines or built from variables slips past.
    assert "container_name" not in _load(BASE)["services"]["app"]
    assert "container_name" not in (prod["services"]["app"] or {})
    assert prod.get("name") == "gdx", "prod overlay must name the project gdx"
    code = [ln for ln in (REPO_ROOT / DOCKER / "update.sh").read_text().splitlines() if not ln.lstrip().startswith("#")]
    projects = {m for ln in code for m in re.findall(r"(?i:\bcompose\b).*?(?:[\s(]-p|--project-name)[=\s]*([^\s)]+)", ln)}
    assert projects == {"gdx"}, f"update.sh runs compose as project(s) {sorted(projects)}, not gdx"
    assert not any("COMPOSE_PROJECT_NAME" in ln for ln in code), "update.sh sets the project name another way"


def test_caddy_never_starts_in_prod_or_dev() -> None:
    # Prod's gdx-nginx container and a dev box's own services hold 80/443; the profile
    # names are never enabled anywhere.
    for rel in (PROD, DEV):
        assert _load(rel)["services"]["caddy"].get("profiles"), f"{rel} does not disable caddy"
