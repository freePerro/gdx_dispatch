"""update.sh refuses to update onto a database volume that does not exist.

An update runs over an existing install. If the compose files it assembles
(COMPOSE_FILE from .env, plus EXTRA_COMPOSE) resolve the database to a volume
that is not on the host, they are the wrong files. The 2026-10-19 audit of
the one layout ran exactly that case: the old deploy command
(`EXTRA_COMPOSE=…serverports.yml`, no COMPOSE_FILE) put prod's database on a
new, empty `gdx_gdx_db_data`, bootstrap seeded it and /health went green while
the real data sat detached. update.sh now stops before its snapshot.

The tests run the real script against a fake `docker` that answers
`compose config` and `volume inspect` and logs every call, so a refusal can
be told apart from a run that reached the database.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_SH = REPO_ROOT / "gdx_dispatch" / "docker" / "update.sh"

FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "$*" >> "$DOCKER_LOG"
case "$*" in
  *" config --format json"*)
    [ -z "$DB_VOLUME" ] && { echo '{"volumes": {}}'; exit 0; }
    printf '{\n  "volumes": {\n    "gdx_db_data": {\n      "name": "%s",\n      "external": true\n    }\n  }\n}\n' "$DB_VOLUME" ;;
  "volume inspect "*) [ "$3" = "$EXISTING_VOLUME" ] && exit 0; exit 1 ;;
  *" exec "*)  # the snapshot: by default stop the run here, it got past the guard
    [ "${SNAPSHOT_OK:-0}" = 1 ] || exit 3
    echo "-- dump" ;;
  *" config app")  # real `config app` output also carries app's dependencies
    printf 'name: gdx\nservices:\n  db:\n    image: postgres:16-alpine\n'
    printf '  app:\n    depends_on:\n      db:\n        condition: service_healthy\n'
    printf '    image: ghcr.io/example/gdx:1.0.0\n'
    printf '  secrets-init:\n    image: ghcr.io/example/gdx:1.0.0\nvolumes:\n  gdx_db_data:\n    name: x\n' ;;
  "run --rm --entrypoint test ghcr.io/example/gdx:1.0.0 -x /init")
    case "${HAS_INIT:-1}" in 1) exit 0 ;; 0) exit 1 ;; *) exit 125 ;; esac ;;
  "run "*) exit 125 ;;  # any other image reference is the lookup gone wrong
  *" ps -q app") echo appid ;;
  "inspect -f {{.State.Health.Status}} appid") echo healthy ;;
esac
exit 0
"""


def _run(
    tmp_path: Path, env_lines: str, db_volume: str, existing: str, **fake_env: str
) -> tuple[int, str, list[str]]:
    root = tmp_path / "repo"
    (root / "gdx_dispatch" / "docker").mkdir(parents=True)
    shutil.copy(UPDATE_SH, root / "gdx_dispatch" / "docker" / "update.sh")
    (root / ".env").write_text(env_lines)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(0o755)
    log = tmp_path / "docker.log"
    log.touch()
    proc = subprocess.run(
        ["bash", str(root / "gdx_dispatch" / "docker" / "update.sh")],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "DOCKER_LOG": str(log),
            "DB_VOLUME": db_volume,
            "EXISTING_VOLUME": existing,
            **fake_env,
        },
    )
    return proc.returncode, proc.stderr, log.read_text().splitlines()


def test_refuses_when_the_database_volume_does_not_exist(tmp_path: Path) -> None:
    rc, err, calls = _run(tmp_path, "", db_volume="gdx_gdx_db_data", existing="docker_db_data")
    assert rc == 1
    assert "Refusing" in err and "gdx_gdx_db_data" in err
    assert not any(" exec " in c or " pull" in c or " up " in c for c in calls), calls


def test_refuses_when_no_database_volume_resolves(tmp_path: Path) -> None:
    rc, err, calls = _run(tmp_path, "", db_volume="", existing="docker_db_data")
    assert rc == 1
    assert "Refusing" in err
    assert not any(" exec " in c for c in calls), calls


def test_proceeds_to_the_snapshot_when_the_volume_exists(tmp_path: Path) -> None:
    rc, err, calls = _run(tmp_path, "", db_volume="docker_db_data", existing="docker_db_data")
    assert "Refusing" not in err
    assert any(" exec -T db pg_dump" in c for c in calls), calls
    assert rc != 0  # the fake's snapshot exits 3; reaching it is the point


def test_compose_file_quotes_are_stripped_like_compose_does(tmp_path: Path) -> None:
    _, _, calls = _run(
        tmp_path,
        'COMPOSE_FILE="a.yml:b.yml"\n',
        db_volume="docker_db_data",
        existing="docker_db_data",
    )
    config = next(c for c in calls if " config --format json" in c)
    assert "-f a.yml -f b.yml" in config, config
    assert '"' not in config, config


# The one-container layout (2026-10-19): the compose files start the app with
# /init, which only images from that change on carry.


def test_refuses_an_image_without_s6_before_anything_is_recreated(tmp_path: Path) -> None:
    rc, err, calls = _run(
        tmp_path, "", db_volume="docker_db_data", existing="docker_db_data", SNAPSHOT_OK="1", HAS_INIT="0"
    )
    assert rc == 1
    assert "has no /init" in err and "ghcr.io/example/gdx:1.0.0" in err
    assert not any(" up " in c for c in calls), calls


def test_a_failed_check_is_not_reported_as_a_missing_init(tmp_path: Path) -> None:
    rc, err, calls = _run(
        tmp_path, "", db_volume="docker_db_data", existing="docker_db_data", SNAPSHOT_OK="1", HAS_INIT="error"
    )
    assert rc == 1
    assert "could not check" in err and "has no /init" not in err
    assert not any(" up " in c for c in calls), calls


def test_the_first_up_removes_the_old_celery_containers(tmp_path: Path) -> None:
    rc, err, calls = _run(tmp_path, "", db_volume="docker_db_data", existing="docker_db_data", SNAPSHOT_OK="1")
    assert rc == 0, err
    ups = [c for c in calls if " up " in c]
    assert ups[0].endswith("up -d --remove-orphans db redis app"), ups
