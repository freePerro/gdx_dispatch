"""Every documented `up` of the base compose creates its external volumes first.

`gdx_dispatch/docker/docker-compose.yml` declares db/uploads `external: true`
so `docker compose down -v` can never delete them. The price is that Compose
will not create them either: on a fresh host `up` aborts with
`external volume "docker_db_data" not found`. The README's Quick start ran that
`up` without the two `docker volume create` lines (GDXA-687), as did the lab,
dev and self-host overlay headers, `dev-up.sh` and the new-host DR runbook.

The guard: in every tracked doc or script, each unit (a markdown section, or a
whole non-markdown file) that runs `up` against the base compose must also say
`docker volume create <name>` for every external volume the compose file
declares. Units that run on a host whose stack already exists are exempt by
name, with the reason, and an exemption that stops matching is itself red.
"""

from __future__ import annotations

import re

import pytest
import yaml

from gdx_dispatch.tools.tracked_files import REPO_ROOT, read_tracked_files

BASE_COMPOSE = "gdx_dispatch/docker/docker-compose.yml"

# `-f …/docker-compose.yml` — the base file, not docker-compose.<x>.yml.
_BASE_REF = re.compile(r"-f\s+(?:\S*/)?docker-compose\.yml(?![\w.])")
# A compose `up` (prose such as "`up` creates a second stack" has no flag).
_UP = re.compile(r"\bup\s+-(?:d\b|-)")
# The runbooks' `COMPOSE=(… -f …docker-compose.yml …)` array, used later.
_ARRAY_UP = re.compile(r'"\$\{COMPOSE\[@\]\}"\s+up\b')
_HEADING = re.compile(r"^#{1,6}\s+(.*\S)\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")

# (path, markdown heading or None for the whole file) -> why it needs no create.
EXEMPT = {
    ("gdx_dispatch/docker/update.sh", None): ("updater for a running stack: it pg_dumps the existing db before any up"),
    ("gdx_dispatch/docs/SECRETS_ROTATION.md", "DB Credential Rotation"): (
        "recreates containers of a running stack after a password change"
    ),
}


def _external_volume_names() -> list[str]:
    spec = yaml.safe_load((REPO_ROOT / BASE_COMPOSE).read_text())
    return sorted(
        (cfg or {}).get("name", key) for key, cfg in (spec.get("volumes") or {}).items() if (cfg or {}).get("external")
    )


def _units(rel: str, text: str) -> list[tuple[str | None, str]]:
    """Markdown splits at headings outside code fences; anything else is one unit."""
    if not rel.endswith(".md"):
        return [(None, text)]
    units: list[tuple[str | None, list[str]]] = [(None, [])]
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        heading = None if in_fence else _HEADING.match(line)
        if heading:
            units.append((heading.group(1), []))
        units[-1][1].append(line)
    return [(h, "\n".join(lines)) for h, lines in units]


def _missing(unit: str, file_text: str, names: list[str]) -> list[str] | None:
    """Volumes the unit's `up` needs but never creates; None if it runs no `up`."""
    direct = _BASE_REF.search(unit) and _UP.search(unit)
    via_array = _ARRAY_UP.search(unit) and _BASE_REF.search(file_text)
    if not (direct or via_array):
        return None
    return [n for n in names if not re.search(rf"docker volume create\s+{re.escape(n)}\b", unit)]


def _scanned() -> list[str]:
    out = []
    for rel in sorted(read_tracked_files(REPO_ROOT)):
        name = rel.rsplit("/", 1)[-1]
        if rel.startswith("docs/design/archive/"):
            continue  # dated records of past decisions, not install paths
        if name.endswith((".md", ".sh", ".yml", ".yaml")) or name.startswith(".env"):
            out.append(rel)
    return out


def _findings() -> tuple[list[str], set[tuple[str, str | None]]]:
    names = _external_volume_names()
    bad, exempt_hit = [], set()
    for rel in _scanned():
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(errors="ignore")
        for heading, unit in _units(rel, text):
            missing = _missing(unit, text, names)
            if missing is None:
                continue
            if (rel, heading) in EXEMPT:
                exempt_hit.add((rel, heading))
            elif missing:
                bad.append(f"{rel} [{heading or 'whole file'}]: no `docker volume create` for {missing}")
    return bad, exempt_hit


def test_base_compose_still_declares_external_volumes():
    # Vacuity check: with no external volumes the guard below proves nothing.
    assert _external_volume_names() == ["docker_db_data", "docker_gdx_uploads"]


def test_every_documented_up_creates_the_external_volumes():
    bad, _ = _findings()
    assert not bad, "\n".join(bad)


def test_every_exemption_still_matches_an_up():
    _, hit = _findings()
    assert set(EXEMPT) - hit == set(), "stale EXEMPT entries: drop them"


@pytest.mark.parametrize(
    "text, expect",
    [
        # The README Quick start as it stood before GDXA-687.
        ("cp .env.template .env\ndocker compose -f gdx_dispatch/docker/docker-compose.yml up -d --build", ["a", "b"]),
        ("docker volume create a\ndocker volume create b\ndocker compose -f x/docker-compose.yml up -d", []),
        ("docker compose -f x/docker-compose.customer.yml up -d", None),
        ("plain `docker compose -f …docker-compose.yml` then `up` creates a second stack", None),
    ],
)
def test_guard_can_fail(text, expect):
    assert _missing(text, text, ["a", "b"]) == expect


def test_markdown_split_ignores_comments_inside_fences():
    md = "## One\n```bash\n# not a heading\ndocker compose -f docker-compose.yml up -d\n```\n## Two\n"
    headings = [h for h, _ in _units("x.md", md)]
    assert headings == [None, "One", "Two"]
