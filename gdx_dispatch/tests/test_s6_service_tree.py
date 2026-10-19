"""The app container's s6 tree runs web, both workers and beat, as wired.

Since 2026-10-19 one app container runs uvicorn, the two Celery workers and
beat under s6-overlay (gdx_dispatch/docker/s6-overlay/README.md), replacing
the celery-high, celery-low and celery-beat services. Most ways to break that
are silent until a container boots: a service directory s6 never enables, a
`run` file the build leaves without its executable bit, or the web command
drifting from the Dockerfile CMD that a plain `docker run` of the image still
uses. These are file checks; the boot itself is tested by hand and on vps2.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

DOCKER = Path(__file__).resolve().parents[1] / "docker"
S6 = DOCKER / "s6-overlay"
RC = S6 / "s6-rc.d"
LONGRUNS = ["svc-web", "svc-worker-high", "svc-worker-low", "svc-beat"]
ROLES = {"svc-web": "web", "svc-worker-high": "worker-high", "svc-worker-low": "worker-low", "svc-beat": "beat"}


def _dockerfile_cmd() -> list[str]:
    text = (DOCKER / "Dockerfile").read_text()
    match = re.search(r"^CMD (\[.*?\])\s*$", text, re.M | re.S)
    assert match, "the Dockerfile has no exec-form CMD"
    return json.loads(match.group(1).replace("\\\n", ""))


def _gdx_run_web_args(reload: bool) -> list[str]:
    """What gdx-run would exec for `web`, read by running its case statement
    under sh with the final exec swapped for printing the arguments."""
    script = (S6 / "scripts" / "gdx-run").read_text()
    # Cut before `cd /app`: CI runs pytest on a host without /app.
    head, cut, tail = script.partition("\ncd /app\n")
    assert cut and tail.startswith("exec /command/s6-setuidgid"), "gdx-run no longer ends cd /app; exec"
    body = head.split("\n", 1)[1]  # drop the with-contenv shebang
    out = subprocess.run(
        ["sh", "-c", body + '\nprintf "%s\\n" "$@"', "gdx-run", "web"],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GDX_DEV_RELOAD": "1" if reload else ""},
    )
    return out.stdout.splitlines()


def test_the_web_process_runs_the_dockerfile_cmd():
    assert _gdx_run_web_args(reload=False) == _dockerfile_cmd()


def test_reload_mode_swaps_in_uvicorn_reload():
    args = _gdx_run_web_args(reload=True)
    assert args[0] == "uvicorn" and "--reload" in args, args


@pytest.mark.parametrize("svc", LONGRUNS)
def test_every_process_is_a_longrun_enabled_after_init(svc):
    d = RC / svc
    assert (d / "type").read_text().strip() == "longrun"
    assert (d / "dependencies.d" / "init-gdx").is_file(), f"{svc} could start before migrations"
    assert (d / "dependencies.d" / "base").is_file()
    assert (S6 / "user-bundles.d" / "user" / "contents.d" / svc).is_file(), f"s6 never starts {svc}"
    for name in ("run", "finish"):
        assert os.access(d / name, os.X_OK), f"{svc}/{name} is not executable"
    run = (d / "run").read_text()
    assert f"exec /etc/s6-overlay/scripts/gdx-run {ROLES[svc]}" in run, run
    assert int((d / "timeout-kill").read_text()) > 0


def test_init_runs_once_and_is_enabled():
    d = RC / "init-gdx"
    assert (d / "type").read_text().strip() == "oneshot"
    assert (d / "up").read_text().strip() == "/etc/s6-overlay/scripts/init-gdx"
    assert (S6 / "user-bundles.d" / "user" / "contents.d" / "init-gdx").is_file()


def test_no_service_directory_is_left_out_of_the_bundle():
    enabled = {p.name for p in (S6 / "user-bundles.d" / "user" / "contents.d").iterdir()}
    defined = {p.name for p in RC.iterdir() if p.is_dir() and p.name != "user"}
    assert defined == enabled == {"init-gdx", *LONGRUNS}


@pytest.mark.parametrize("script", sorted(p.name for p in (S6 / "scripts").iterdir()))
def test_every_script_is_executable(script):
    assert os.access(S6 / "scripts" / script, os.X_OK)


def test_the_healthcheck_covers_every_process():
    text = (S6 / "scripts" / "healthcheck").read_text()
    loop = re.search(r"^for svc in (.+); do$", text, re.M)
    assert loop and shlex.split(loop.group(1)) == LONGRUNS


def test_the_workers_run_without_migrating_or_bootstrapping():
    """Only init-gdx migrates; four processes racing `alembic upgrade head`
    is what the split layout's GDX_SKIP_MIGRATIONS on the workers prevented."""
    text = (S6 / "scripts" / "gdx-run").read_text()
    assert "GDX_SKIP_MIGRATIONS=1 GDX_SKIP_BOOTSTRAP=1" in text


def test_compose_starts_the_app_container_under_s6():
    services = yaml.safe_load((DOCKER / "docker-compose.yml").read_text())["services"]
    app = services["app"]
    assert app["entrypoint"] == ["/init"]
    assert app["healthcheck"]["test"] == ["CMD", "/etc/s6-overlay/scripts/healthcheck"]
    assert not [s for s in services if s.startswith("celery")], "a separate Celery service is back"
