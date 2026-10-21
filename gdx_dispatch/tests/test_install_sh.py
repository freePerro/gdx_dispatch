"""install.sh installs and updates without touching data it should not.

Since 2026-10-19 a release publishes `install.sh` beside the compose file
(release.yml stamps GDX_RELEASE with the tag). One command installs or, run
again, updates. The ways it can do harm are quiet ones: taking over a `gdx`
stack that runs from another directory, starting an update before the
snapshot, pinning an image that does not match the compose file, or dropping
a setting from `.env`.

The tests run the real script under `sh` (dash in the test image, as
`curl | sh` runs it on Debian and Ubuntu) against a fake `docker`, `curl`,
`ss`, `ip` and `sleep` that log every call. Most pass it as a file; the
piped tests feed it on stdin the way the README's one-liner does, with a fake
`compose exec` that reads stdin as the real one does. The real install is
rehearsed on vps2.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "gdx_dispatch" / "docker" / "install.sh"
RELEASE_YML = REPO_ROOT / ".github" / "workflows" / "release.yml"

BANNER = """app-1  | WARNING bootstrap
app-1  | ════════════════════════════════════════════════════════════
app-1  |   GDX Dispatch — initial admin account created
app-1  |     email:    owner@example.com
app-1  |     password: Gen3rated-Secret
app-1  |   You MUST change this password on first login.
"""

FAKE_DOCKER = r"""#!/bin/sh
echo "docker $*" >> "$CALL_LOG"
case "$*" in
  info) exit 0 ;;
  "version --format"*) echo "${FAKE_ENGINE:-27.3.1}" ;;
  "compose version --short") echo "${FAKE_COMPOSE-2.29.7}" ;;
  "compose up --help") [ "${FAKE_NO_UP_PULL:-0}" = 1 ] || echo "      --pull string   Pull image before running" ;;
  "ps -a --filter"*) [ -n "${FAKE_OTHER_DIR:-}" ] && echo "$FAKE_OTHER_DIR" ;;
  "volume inspect gdx_gdx_db_data") [ "${FAKE_EXISTING:-0}" = 1 ] || exit 1 ;;
  *" up -d db"|*" up -d --pull never db") f="$(printf '%s\n' "$@" | sed -n '/^-f$/{n;p;}')"; echo "db started with: $(cat "$f")" >> "$CALL_LOG" ;;
  # compose exec forwards stdin even with -T: under `curl | sh` that is the script.
  *" exec -T db pg_isready"*) cat > /dev/null ;;
  *" exec -T db pg_dump"*) cat > /dev/null; echo "-- dump" ;;
  *" pull") [ "${FAKE_PULL_FAIL:-0}" = 1 ] && exit 1 ;;
  *" ps -q app") echo appid ;;
  *" port caddy 443") printf '%s\n' "${FAKE_HTTPS_BIND-0.0.0.0:443}" ;;
  "inspect -f {{.State.Health.Status}} appid") echo "${FAKE_HEALTH:-healthy}" ;;
  # A recreated container's logs start empty: the first boot's banner is gone.
  *" up -d --pull never"|*" up -d") : > "$CALL_LOG.recreated" ;;
  *" logs app") [ -e "$CALL_LOG.recreated" ] || printf '%s' "$FAKE_BANNER" ;;
esac
exit 0
"""

FAKE_CURL = r"""#!/bin/sh
echo "curl $*" >> "$CALL_LOG"
case "$*" in *https://*/health*) [ "${FAKE_HTTPS_FAIL:-0}" = 1 ] && exit 7 ;; esac
while [ $# -gt 0 ]; do
  [ "$1" = "-o" ] && { echo "services: {}" > "$2"; exit 0; }
  shift
done
"""

FAKE_SS = r"""#!/bin/sh
case "$*" in *":${FAKE_BUSY_PORT:-none}"*) echo "LISTEN 0 4096 0.0.0.0:x" ;; esac
"""

FAKE_IP = r"""#!/bin/sh
echo "1.1.1.1 via 10.0.0.1 dev eth0 src ${FAKE_SRC:-203.0.113.5} uid 0"
"""


def _install(tmp_path: Path, *args: str, script: Path = INSTALL_SH, shell: str = "sh", piped: bool = False, **fake: str):
    bin_dir = tmp_path / "bin"
    if not bin_dir.exists():
        bin_dir.mkdir()
        for name, body in {
            "docker": FAKE_DOCKER,
            "curl": FAKE_CURL,
            "ss": FAKE_SS,
            "ip": FAKE_IP,
            "sleep": "#!/bin/sh\n",
        }.items():
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    target = tmp_path / "opt"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "CALL_LOG": str(log),
        "GDX_DIR": str(target),
        "FAKE_BANNER": BANNER,
        **fake,
    }
    if piped:  # curl … | sudo sh -s -- ARGS
        cmd, stdin = [shell, "-s", "--", "--yes", *args], script.read_text()
    else:
        cmd, stdin = [shell, str(script), "--yes", *args], None
    proc = subprocess.run(
        cmd,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=None if piped else subprocess.DEVNULL,
        env=env,
        cwd=tmp_path,
    )
    return proc, log.read_text().splitlines(), target


def _env(target: Path) -> list[str]:
    return (target / ".env").read_text().splitlines()


def _index(calls: list[str], needle: str) -> int:
    return next(i for i, c in enumerate(calls) if needle in c)


def test_a_fresh_install_pins_the_release_and_starts_the_stack(tmp_path):
    proc, calls, target = _install(
        tmp_path, "--version", "v1.4.2", "--domain", "dispatch.example.com", "--email", "owner@example.com"
    )
    assert proc.returncode == 0, proc.stderr
    assert sorted(_env(target)) == [
        "APP_VERSION=1.4.2",
        "GDX_ADMIN_EMAIL=owner@example.com",
        "GDX_DOMAIN=dispatch.example.com",
    ]
    assert stat.S_IMODE(os.stat(target / ".env").st_mode) == 0o600
    assert any("/releases/download/v1.4.2/docker-compose.customer.yml" in c for c in calls), calls
    compose = "docker compose -p gdx --env-file .env -f docker-compose.yml"
    staged = "docker compose -p gdx --env-file .env.new -f docker-compose.yml.new"
    # pull_policy: always would pull again on `up` without --pull never.
    assert _index(calls, f"{staged} pull") < _index(calls, f"{compose} up -d --remove-orphans --pull never")
    assert not any("pg_dump" in c for c in calls), "a fresh install has nothing to snapshot"
    # A whole line, not a substring: the URL must be what the script prints.
    assert "[gdx] GDX Dispatch 1.4.2 is running at https://dispatch.example.com" in proc.stdout.splitlines()
    assert "password: Gen3rated-Secret" in proc.stdout


def test_without_a_domain_it_serves_https_on_the_public_ip(tmp_path):
    proc, calls, target = _install(tmp_path, "--version", "1.4.2")
    assert proc.returncode == 0, proc.stderr
    assert "GDX_PUBLIC_IP=203.0.113.5" in _env(target)
    probe = _index(calls, "curl -fsS -o /dev/null --connect-timeout 5 --max-time 10 --connect-to 203.0.113.5:443:127.0.0.1:443 https://203.0.113.5/health")
    assert probe > _index(calls, "inspect -f {{.State.Health.Status}} appid")
    assert "running at https://203.0.113.5" in proc.stdout
    assert "15 minutes" not in proc.stderr


def test_when_the_ip_certificate_fails_it_goes_back_to_plain_http(tmp_path):
    proc, calls, target = _install(tmp_path, "--version", "1.4.2", FAKE_HTTPS_FAIL="1")
    assert proc.returncode == 0, proc.stderr
    assert "GDX_PUBLIC_IP=" in _env(target)
    assert not any(line.startswith("GDX_PUBLIC_IP=2") for line in _env(target))
    probes = [i for i, c in enumerate(calls) if "https://203.0.113.5/health" in c]
    assert len(probes) == 24
    compose = "docker compose -p gdx --env-file .env -f docker-compose.yml"
    restart = calls.index(f"{compose} up -d --pull never")
    assert restart > probes[-1]
    assert _index(calls[restart:], "inspect -f {{.State.Health.Status}} appid") >= 0
    assert "running at http://203.0.113.5" in proc.stdout
    assert "rerun with --ip 203.0.113.5" in proc.stderr
    assert "15 minutes" in proc.stderr
    # The fallback recreates app; the password was read before that (vps2
    # rehearsal 2, 2026-10-19: it was lost and the hint found nothing).
    assert "password: Gen3rated-Secret" in proc.stdout


@pytest.mark.parametrize("src", ["192.168.1.20", "10.1.2.3", "172.20.0.4", "100.101.139.69"])
def test_a_private_address_is_not_given_to_lets_encrypt(tmp_path, src):
    proc, calls, target = _install(tmp_path, "--version", "1.4.2", FAKE_SRC=src)
    assert proc.returncode == 0, proc.stderr
    assert "GDX_PUBLIC_IP=" in _env(target)
    assert not any("/health" in c for c in calls), calls
    assert f"running at http://{src}" in proc.stdout
    assert "15 minutes" in proc.stderr


def test_the_ip_is_decided_once_and_the_flag_changes_it(tmp_path):
    target = tmp_path / "opt"
    target.mkdir()
    (target / ".env").write_text("APP_VERSION=1.0.0\nGDX_PUBLIC_IP=\n")
    (target / "docker-compose.yml").write_text("services: {}\n")
    proc, calls, _ = _install(tmp_path, "--version", "1.5.0", FAKE_EXISTING="1")
    assert proc.returncode == 0, proc.stderr
    assert sorted(_env(target)) == ["APP_VERSION=1.5.0", "GDX_PUBLIC_IP="]
    assert not any("/health" in c for c in calls), calls

    # Snapshot names carry the second; three runs in one second would collide.
    shutil.rmtree(target / "backups")
    proc, calls, _ = _install(tmp_path, "--version", "1.5.0", "--ip", "198.51.100.9", FAKE_EXISTING="1")
    assert proc.returncode == 0, proc.stderr
    assert "GDX_PUBLIC_IP=198.51.100.9" in _env(target)
    assert "running at https://198.51.100.9" in proc.stdout

    shutil.rmtree(target / "backups")

    proc, calls, _ = _install(tmp_path, "--version", "1.5.0", "--ip", "none", FAKE_EXISTING="1")
    assert proc.returncode == 0, proc.stderr
    assert "GDX_PUBLIC_IP=" in _env(target)
    assert "running at http://" in proc.stdout

    # A domain added later wins over a stored address.
    (target / ".env").write_text("APP_VERSION=1.5.0\nGDX_PUBLIC_IP=198.51.100.9\n")
    shutil.rmtree(target / "backups")
    proc, calls, _ = _install(tmp_path, "--version", "1.5.0", "--domain", "d.example.com", FAKE_EXISTING="1")
    assert proc.returncode == 0, proc.stderr
    assert not any("/health" in c for c in calls), calls
    assert "running at https://d.example.com" in proc.stdout


@pytest.mark.parametrize(
    ("bind", "target"),
    [
        ("0.0.0.0:443", "127.0.0.1:443"),
        ("0.0.0.0:8443\n[::]:8443", "127.0.0.1:8443"),
        ("198.51.100.9:443", "198.51.100.9:443"),
        ("", "127.0.0.1:443"),
    ],
)
def test_the_probe_dials_where_compose_published_https(tmp_path, bind, target):
    # GDX_HTTPS_PORT can move 443 or bind it to one address, and a probe that
    # dials the wrong place fails 24 times and drops an issued certificate to
    # plain HTTP. Parsing .env got compose's rules wrong three ways (Build D
    # re-audits, 2026-10-19), so the script asks `compose port`; a wildcard
    # bind answers on loopback, a non-loopback one only on its own address.
    proc, calls, _ = _install(tmp_path, "--version", "1.4.2", FAKE_HTTPS_BIND=bind)
    assert proc.returncode == 0, proc.stderr
    assert _index(calls, "port caddy 443") < _index(calls, "https://203.0.113.5/health")
    assert _index(calls, f"--connect-to 203.0.113.5:443:{target} https://203.0.113.5/health") >= 0
    assert "running at https://203.0.113.5" in proc.stdout


def test_a_domain_wins_over_the_ip(tmp_path):
    proc, calls, target = _install(tmp_path, "--version", "1.4.2", "--domain", "d.example.com")
    assert proc.returncode == 0, proc.stderr
    assert not any(line.startswith("GDX_PUBLIC_IP") for line in _env(target))
    assert not any("/health" in c for c in calls), calls


def test_the_release_copy_pins_its_own_tag_and_an_unstamped_copy_refuses(tmp_path):
    stamped = tmp_path / "install.sh"
    text = INSTALL_SH.read_text()
    assert text.count('GDX_RELEASE="__GDX_RELEASE__"') == 1
    stamped.write_text(text.replace('GDX_RELEASE="__GDX_RELEASE__"', 'GDX_RELEASE="v2.0.0"'))
    proc, calls, target = _install(tmp_path, script=stamped)
    assert proc.returncode == 0, proc.stderr
    assert "APP_VERSION=2.0.0" in _env(target)
    assert any("/releases/download/v2.0.0/" in c for c in calls), calls

    proc, calls, _ = _install(tmp_path)  # the repo copy, no --version
    assert proc.returncode == 1
    assert "not from a release" in proc.stderr
    assert not any(" up -d" in c for c in calls), calls


def test_an_update_snapshots_before_it_pulls_and_keeps_the_settings(tmp_path):
    target = tmp_path / "opt"
    target.mkdir()
    (target / ".env").write_text("GDX_DOMAIN=dispatch.example.com\nSTRIPE_SECRET_KEY=sk_x\nAPP_VERSION=1.0.0\n")
    (target / ".env").chmod(0o644)  # as the README's old `echo > .env` left it
    (target / "docker-compose.yml").write_text("services: {old: {}}\n")
    # An update over a live stack: ports 80/443 are in use by its own Caddy.
    proc, calls, _ = _install(tmp_path, "--version", "1.5.0", FAKE_EXISTING="1", FAKE_BUSY_PORT="80")
    assert proc.returncode == 0, proc.stderr
    assert "db started with: services: {old: {}}" in calls, "the snapshot runs on the files the install ran"
    # The old files may set pull_policy: always; the snapshot must not pull.
    assert any(c.endswith(" up -d --pull never db") for c in calls), calls
    assert _index(calls, "pg_isready") < _index(calls, "pg_dump") < _index(calls, " pull")
    assert _index(calls, " pull") < _index(calls, "up -d --remove-orphans")
    assert sorted(_env(target)) == ["APP_VERSION=1.5.0", "GDX_DOMAIN=dispatch.example.com", "STRIPE_SECRET_KEY=sk_x"]
    snaps = list((target / "backups").glob("gdx-pre-update-*.sql.gz"))
    assert len(snaps) == 1
    assert stat.S_IMODE(os.stat(target / ".env").st_mode) == 0o600
    assert (target / "docker-compose.yml").read_text() == "services: {}\n"
    assert "password:" not in proc.stdout, "an update does not reprint an old banner"


def test_a_failed_snapshot_stops_before_anything_changes(tmp_path):
    bin_dir = tmp_path / "bin"
    _install(tmp_path, "--version", "1.5.0")  # creates the fakes
    fake = (bin_dir / "docker").read_text().replace('echo "-- dump" ;;', "exit 1 ;;")
    (bin_dir / "docker").write_text(fake)
    target = tmp_path / "opt"
    shutil.rmtree(target)  # the run above was a fresh install there
    target.mkdir()
    (target / ".env").write_text("APP_VERSION=1.0.0\n")
    (target / "docker-compose.yml").write_text("services: {old: {}}\n")
    before = sorted(p.name for p in target.iterdir())
    proc, calls, _ = _install(tmp_path, "--version", "1.5.0", FAKE_EXISTING="1")
    assert proc.returncode == 1
    assert "snapshot failed" in proc.stderr
    assert not any(" pull" in c or "--remove-orphans" in c for c in calls), calls
    # "nothing was changed" holds: same files, same pin, no partial dump.
    assert sorted(p.name for p in target.iterdir()) == sorted([*before, "backups"])
    assert _env(target) == ["APP_VERSION=1.0.0"]
    assert (target / "docker-compose.yml").read_text() == "services: {old: {}}\n"
    assert list((target / "backups").iterdir()) == []


def test_an_install_from_the_old_readme_is_snapshotted_on_its_own_file(tmp_path):
    """Before install.sh the README saved docker-compose.customer.yml, .env optional."""
    target = tmp_path / "opt"
    target.mkdir()
    (target / "docker-compose.customer.yml").write_text("services: {old: {}}\n")
    proc, calls, _ = _install(tmp_path, "--version", "1.5.0", FAKE_EXISTING="1")
    assert proc.returncode == 0, proc.stderr
    assert "db started with: services: {old: {}}" in calls
    db_up = next(c for c in calls if c.endswith(" up -d --pull never db"))
    assert "--env-file /dev/null -f docker-compose.customer.yml" in db_up
    assert sorted(p.name for p in target.iterdir()) == [".env", "backups", "docker-compose.yml"]
    assert _env(target) == ["APP_VERSION=1.5.0", "GDX_PUBLIC_IP=203.0.113.5"]


def test_a_failed_pull_leaves_the_old_files_in_place(tmp_path):
    target = tmp_path / "opt"
    target.mkdir()
    (target / ".env").write_text("APP_VERSION=1.0.0\n")
    (target / "docker-compose.yml").write_text("services: {old: {}}\n")
    proc, calls, _ = _install(tmp_path, "--version", "1.5.0", FAKE_EXISTING="1", FAKE_PULL_FAIL="1")
    assert proc.returncode == 1
    assert "pull failed" in proc.stderr
    assert not any("--remove-orphans" in c for c in calls), calls
    assert _env(target) == ["APP_VERSION=1.0.0"]
    assert (target / "docker-compose.yml").read_text() == "services: {old: {}}\n"
    assert sorted(p.name for p in target.iterdir()) == [".env", "backups", "docker-compose.yml"]


@pytest.mark.parametrize("shell", ["sh", "bash"])  # bash is sh on RHEL, Rocky, Alma, Fedora
def test_piped_into_the_shell_an_update_runs_to_the_end(tmp_path, shell):
    """`curl | sudo sh`: stdin is the script, and compose exec reads stdin."""
    target = tmp_path / "opt"
    target.mkdir()
    (target / ".env").write_text("APP_VERSION=1.0.0\n")
    (target / "docker-compose.yml").write_text("services: {old: {}}\n")
    proc, calls, _ = _install(tmp_path, "--version", "1.5.0", shell=shell, piped=True, FAKE_EXISTING="1")
    assert proc.returncode == 0, proc.stderr
    assert any("pg_dump" in c for c in calls), calls
    assert any(c.endswith("up -d --remove-orphans --pull never") for c in calls), calls
    assert "is running at" in proc.stdout
    assert _env(target) == ["APP_VERSION=1.5.0", "GDX_PUBLIC_IP=203.0.113.5"]


@pytest.mark.parametrize("shell", ["sh", "bash"])
def test_piped_into_the_shell_a_fresh_install_prints_the_admin_login(tmp_path, shell):
    proc, _, _ = _install(tmp_path, "--version", "1.5.0", shell=shell, piped=True)
    assert proc.returncode == 0, proc.stderr
    assert "password: Gen3rated-Secret" in proc.stdout


def test_refuses_a_gdx_stack_that_runs_from_another_directory(tmp_path):
    proc, calls, _ = _install(tmp_path, "--version", "1.5.0", FAKE_OTHER_DIR="/root/gdx")
    assert proc.returncode == 1
    assert "/root/gdx" in proc.stderr
    assert not any("docker compose" in c and " up -d" in c for c in calls), calls


def test_a_stack_started_through_a_symlink_to_the_install_dir_is_not_another_install(tmp_path):
    (tmp_path / "opt").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "opt")
    proc, _, _ = _install(tmp_path, "--version", "1.5.0", FAKE_OTHER_DIR=str(tmp_path / "alias"))
    assert proc.returncode == 0, proc.stderr


def test_a_failed_pull_on_a_fresh_install_says_nothing_was_installed(tmp_path):
    proc, _, target = _install(tmp_path, "--version", "1.5.0", FAKE_PULL_FAIL="1")
    assert proc.returncode == 1
    assert "nothing was installed" in proc.stderr
    assert list(target.iterdir()) == []


def test_refuses_a_checkout_that_update_sh_manages(tmp_path):
    target = tmp_path / "opt"
    target.mkdir()
    env = "COMPOSE_FILE=gdx_dispatch/docker/docker-compose.yml:gdx_dispatch/docker/docker-compose.prod.yml\n"
    (target / ".env").write_text(env)
    proc, calls, _ = _install(tmp_path, "--version", "1.5.0", FAKE_EXISTING="1")
    assert proc.returncode == 1
    assert "update.sh" in proc.stderr
    assert (target / ".env").read_text() == env
    assert not (target / "docker-compose.yml").exists()
    assert not any("docker compose -p" in c for c in calls), calls


def test_a_fresh_install_refuses_a_busy_port(tmp_path):
    proc, calls, _ = _install(tmp_path, "--version", "1.5.0", FAKE_BUSY_PORT="443")
    assert proc.returncode == 1
    assert "port 443" in proc.stderr
    assert not any(" up -d" in c for c in calls), calls


def test_refuses_old_docker_and_bad_values(tmp_path):
    proc, _, _ = _install(tmp_path, "--version", "1.5.0", FAKE_ENGINE="20.10.24")
    assert proc.returncode == 1 and "too old" in proc.stderr
    proc, _, _ = _install(tmp_path, "--version", "1.5.0", FAKE_COMPOSE="")
    assert proc.returncode == 1 and "Compose v2" in proc.stderr
    for args in (
        ["--domain", "a b.example.com"],
        ["--email", "x$(id)@example.com"],
        ["--email", "nobody"],
        ["--ip", "1.2.3"],
        ["--ip", "1.2.3.4.5"],
        ["--ip", "1.2.3.4;id"],
        ["--ip", "::1"],
        ["--ip", "999.1.1.1"],
        ["--ip", "1.2.3.0004"],
    ):
        proc, calls, _ = _install(tmp_path, "--version", "1.5.0", *args)
        assert proc.returncode == 1, args
        assert not calls or not any("docker compose -p" in c for c in calls), args


def test_an_install_with_no_compose_file_snapshots_on_the_new_one(tmp_path):
    """Data volumes but no compose file: the database starts on the new file.

    That file names the new version, whose images are pulled only after the
    snapshot, so this one start may pull; with --no-pull it may not.
    """
    target = tmp_path / "opt"
    target.mkdir()
    proc, calls, _ = _install(tmp_path, "--version", "1.5.0", FAKE_EXISTING="1")
    assert proc.returncode == 0, proc.stderr
    db_up = next(c for c in calls if c.endswith(" up -d db"))
    assert "-f docker-compose.yml.new" in db_up
    shutil.rmtree(target)
    target.mkdir()
    (tmp_path / "branch-compose.yml").write_text("services: {branch: {}}\n")
    proc, calls, _ = _install(
        tmp_path, "--version", "1.5.0", "--compose-file", "branch-compose.yml", "--no-pull", FAKE_EXISTING="1"
    )
    assert proc.returncode == 0, proc.stderr
    assert any(c.endswith(" up -d --pull never db") for c in calls), calls


def test_a_local_compose_file_and_loaded_image_skip_the_download_and_pull(tmp_path):
    local = tmp_path / "branch-compose.yml"
    local.write_text("services: {branch: {}}\n")
    proc, calls, target = _install(  # relative to where it is run, not to the install dir
        tmp_path, "--version", "one-layout-test", "--compose-file", "branch-compose.yml", "--no-pull"
    )
    assert proc.returncode == 0, proc.stderr
    assert (target / "docker-compose.yml").read_text() == local.read_text()
    assert not any("/releases/download/" in c for c in calls), calls
    assert not any(c.endswith(" pull") for c in calls), calls
    assert "APP_VERSION=one-layout-test" in _env(target)
    assert any(c.endswith("up -d --remove-orphans --pull never") for c in calls), calls
    shutil.rmtree(target)
    proc, calls, _ = _install(
        tmp_path, "--version", "one-layout-test", "--compose-file", "branch-compose.yml", "--no-pull",
        FAKE_NO_UP_PULL="1",
    )
    assert not target.exists() or list(target.iterdir()) == []
    assert proc.returncode == 1 and "--no-pull needs a newer one" in proc.stderr
    assert not any(" up -d" in c for c in calls), "refused before anything ran"


def test_an_unhealthy_app_fails_the_run(tmp_path):
    proc, _, _ = _install(tmp_path, "--version", "1.5.0", FAKE_HEALTH="starting", GDX_HEALTH_TIMEOUT="20")
    assert proc.returncode == 1
    assert "not healthy" in proc.stderr


def test_the_release_step_publishes_a_stamped_install_sh(tmp_path):
    """Run the release step's own asset lines on a copy of the two files."""
    steps = yaml.safe_load(RELEASE_YML.read_text())["jobs"]
    run = next(
        s["run"] for job in steps.values() for s in job["steps"] if s.get("name") == "Create GitHub Release"
    )
    run = run.replace("${{ github.ref_name }}", "v3.1.0")
    asset_lines, _, rest = run.partition('body="$(')
    for rel in ("gdx_dispatch/docker/docker-compose.yml", "gdx_dispatch/docker/install.sh"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / rel, tmp_path / rel)
    subprocess.run(["bash", "-euo", "pipefail", "-c", asset_lines], cwd=tmp_path, check=True)
    assert 'GDX_RELEASE="v3.1.0"' in (tmp_path / "dist" / "install.sh").read_text().splitlines()
    assert "dist/install.sh" in rest.split("gh release create", 1)[1]
