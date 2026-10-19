"""Every compose layout that runs `app` mounts what the base `app` mounts.

`docker-compose.yml` mounts `gdx_recordings:/app/recordings` on app. The
customer stack (`docker-compose.customer.yml`, a standalone file, not an
overlay) never did, so every self-host's session recordings were written to the
container's writable layer and thrown away by the next `pull_policy: always`
recreate (GDXA-1682). Nothing errored. The class is a standalone compose file
drifting from the base app's volume list, and this guard covers it.

Two kinds of file need two checks. A standalone file has its own `app` and must
list every base mount target, or carry an exemption below whose condition is
re-checked here. An overlay is layered on the base with `-f`, and Compose merges
service `volumes` by mount target, so it inherits the base mounts. It can drop
them only with a `!override` or `!reset` tag. That was measured with
`docker compose config` (v5.6.0) on 2026-10-19: base plus dev resolves to
uploads, recordings and the dev source bind.

Every tracked compose file must be classified, so a new layout cannot slip past.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from gdx_dispatch.tools.tracked_files import REPO_ROOT, read_tracked_files

DOCKER = "gdx_dispatch/docker/"
BASE = DOCKER + "docker-compose.yml"

#: Files with their own `app` service, run without the base file.
STANDALONE = [
    DOCKER + "docker-compose.customer.yml",
    DOCKER + "demo/docker-compose.demo.yml",
]
#: Files layered on the base with `-f` (see each file's header).
OVERLAYS = [
    DOCKER + "docker-compose.dev.yml",
    DOCKER + "docker-compose.lab.yml",
    DOCKER + "docker-compose.selfhost.yml",
]
#: Files that run no `app` at all.
NO_APP = [
    DOCKER + "docker-compose.monitoring.yml",
]


def _recording_disabled(app: dict) -> bool:
    value = str((app.get("environment") or {}).get("GDX_SESSION_RECORDING", "1"))
    return value.lower() in ("0", "false", "no")  # routers/browser_proxy.py's test


#: (file, base mount target) -> (why the drift is deliberate, the condition that
#: keeps it safe). The condition is re-checked; when it stops holding, red.
EXEMPT = {
    (DOCKER + "demo/docker-compose.demo.yml", "/app/recordings"): (
        "public demo: visitors' sessions are unscrubbed captures, so recording is off and there is no volume",
        _recording_disabled,
    ),
}


class _TagLoader(yaml.SafeLoader):
    """Safe loader that keeps Compose's `!override` / `!reset` merge tags visible."""


class _Tagged:
    def __init__(self, tag: str, value):
        self.tag, self.value = tag, value


def _construct_tagged(loader, node):
    if isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    elif isinstance(node, yaml.MappingNode):
        value = loader.construct_mapping(node, deep=True)
    else:
        value = loader.construct_scalar(node)
    return _Tagged(node.tag, value)


for _tag in ("!override", "!reset"):
    _TagLoader.add_constructor(_tag, _construct_tagged)


def _load(rel: str) -> dict:
    return yaml.load((REPO_ROOT / rel).read_text(), Loader=_TagLoader) or {}  # noqa: S506 - SafeLoader subclass


def _targets(volumes) -> set[str]:
    """Container paths of a service's mounts, short (`src:target[:mode]`) or long form."""
    out = set()
    for entry in volumes or []:
        if isinstance(entry, dict):
            out.add(entry["target"])
        else:
            parts = str(entry).split(":")
            out.add(parts[1] if len(parts) > 1 else parts[0])
    return out


def _app(rel: str):
    return (_load(rel).get("services") or {}).get("app")


BASE_TARGETS = sorted(_targets(_app(BASE)["volumes"]))


def test_the_base_app_mounts_recordings():
    """Pins the input the guard compares against, so an empty base list cannot pass it vacuously."""
    assert "/app/recordings" in BASE_TARGETS and "/app/uploads" in BASE_TARGETS


def test_every_tracked_compose_file_is_classified():
    tracked = {
        p
        for p in read_tracked_files(REPO_ROOT)
        if p.startswith(DOCKER)
        and pathlib.PurePosixPath(p).name.startswith("docker-compose")
        and p.endswith((".yml", ".yaml"))
    }
    known = {BASE, *STANDALONE, *OVERLAYS, *NO_APP}
    assert tracked - known == set(), (
        f"unclassified compose file(s) {sorted(tracked - known)}: add each to STANDALONE, OVERLAYS or NO_APP "
        "so its app volumes are checked against the base"
    )
    assert known - tracked == set(), f"classified but not tracked: {sorted(known - tracked)}"


@pytest.mark.parametrize("rel", STANDALONE)
@pytest.mark.parametrize("target", BASE_TARGETS)
def test_a_standalone_app_mounts_every_base_target(rel, target):
    app = _app(rel)
    assert app, f"{rel} is listed as standalone but has no app service"
    _check_standalone(rel, app, target)


def _check_standalone(rel: str, app: dict, target: str) -> None:
    if (rel, target) in EXEMPT:
        why, still_safe = EXEMPT[(rel, target)]
        assert still_safe(app), f"{rel}: the exemption for {target} ({why}) no longer holds"
        assert target not in _targets(app.get("volumes")), f"{rel} now mounts {target}; drop its EXEMPT entry"
        return
    assert target in _targets(app.get("volumes")), (
        f"{rel}: app never mounts {target}, which the base app does. Whatever the app writes there lands in the "
        "container's writable layer and is lost on the next recreate"
    )


@pytest.mark.parametrize("rel", OVERLAYS)
def test_an_overlay_never_strips_a_base_mount(rel):
    app = _app(rel)
    if app is not None:
        _check_overlay(rel, app)


def _check_overlay(rel: str, app) -> None:
    assert not isinstance(app, _Tagged), f"{rel} replaces the whole app service with {app.tag}"
    volumes = app.get("volumes")
    if isinstance(volumes, _Tagged):
        missing = sorted(set(BASE_TARGETS) - _targets(volumes.value if volumes.tag == "!override" else []))
        assert not missing, f"{rel}: app volumes {volumes.tag} drops base mount(s) {missing}"


@pytest.mark.parametrize("rel", NO_APP)
def test_a_no_app_file_really_runs_no_app(rel):
    assert _app(rel) is None, f"{rel} now defines app: move it to STANDALONE or OVERLAYS"


def test_the_guard_catches_the_customer_drift_it_was_written_for():
    """Falsifier: the pre-GDXA-1682 customer app mounts must fail the standalone check."""
    rel = DOCKER + "docker-compose.customer.yml"
    app = dict(_app(rel), volumes=["gdx_uploads:/app/uploads", "gdx_secrets:/secrets:ro"])
    with pytest.raises(AssertionError, match="never mounts /app/recordings"):
        _check_standalone(rel, app, "/app/recordings")


def test_the_guard_catches_an_overlay_that_overrides_the_mounts():
    """Falsifier for the overlay check: `volumes: !override` with only a source bind drops both base mounts."""
    for text in ("volumes: !override\n  - ../../gdx_dispatch:/app/gdx_dispatch:ro\n", "volumes: !reset []\n"):
        app = yaml.load(text, Loader=_TagLoader)  # noqa: S506 - SafeLoader subclass
        with pytest.raises(AssertionError, match="drops base mount"):
            _check_overlay("overlay.yml", app)


def test_the_demo_exemption_fails_once_recording_is_on():
    """The exemption is conditional: a demo app that records without a volume is red."""
    rel = DOCKER + "demo/docker-compose.demo.yml"
    app = _app(rel)
    on = dict(app, environment=dict(app["environment"], GDX_SESSION_RECORDING="1"))
    with pytest.raises(AssertionError, match="no longer holds"):
        _check_standalone(rel, on, "/app/recordings")
