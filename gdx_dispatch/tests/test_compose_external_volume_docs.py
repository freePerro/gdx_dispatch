"""Every documented `up` of the dev overlay creates its external volumes first.

An `external: true` volume is one Compose never deletes (`down -v` included)
and never creates: on a fresh host `up` aborts with
`external volume "docker_db_data" not found`. The README's Quick start ran that
`up` without the two `docker volume create` lines (GDXA-687), as did the lab,
dev and self-host overlay headers, `dev-up.sh` and the new-host DR runbook.

Since the one layout (2026-10-19) the base `docker-compose.yml` declares no
external volume at all, so a fresh install creates its own; that is pinned
below, because an external volume in the base is GDXA-687 again on every
customer box. The externals live in the overlays:

- `docker-compose.dev.yml` keeps the dev database on the volumes it always had.
  Every unit that runs `up` with it must say `docker volume create <name>` for
  each of them; that is the guard.
- `docker-compose.prod.yml` maps prod's existing data volumes, and there a
  missing volume aborting `up` is the point (a lost volume must not start an
  empty prod), so nothing may create them. It is not in GUARDED.

The guard: in every tracked doc or script, each unit (a markdown section, or a
whole non-markdown file) that runs `up` against a guarded overlay must also say
`docker volume create <name>` for every external volume that overlay declares.
Units that run on a host whose stack already exists are exempt by name, with
the reason, and an exemption that stops matching is itself red.
"""

from __future__ import annotations

import re

import pytest
import yaml

from gdx_dispatch.tools.tracked_files import REPO_ROOT, read_tracked_files

BASE_COMPOSE = "gdx_dispatch/docker/docker-compose.yml"

# Overlay -> the `-f` reference that pulls it into an `up`.
GUARDED = {
    "gdx_dispatch/docker/docker-compose.dev.yml": re.compile(r"-f\s+(?:\S*/)?docker-compose\.dev\.yml(?![\w.])"),
}
_DEV = "gdx_dispatch/docker/docker-compose.dev.yml"
# A compose `up` (prose such as "`up` creates a second stack" has no flag).
_UP = re.compile(r"\bup\s+-(?:d\b|-)")
_HEADING = re.compile(r"^#{1,6}\s+(.*\S)\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")

# (path, markdown heading or None for the whole file) -> why it needs no create.
# Empty since 2026-10-19: the units it held ran the base, which no longer
# declares an external volume. Kept as the mechanism, and the stale check below
# still guards it.
EXEMPT: dict[tuple[str, str | None], str] = {}


def _external_volume_names(rel: str) -> list[str]:
    spec = yaml.safe_load((REPO_ROOT / rel).read_text())
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


def _missing(unit: str, ref: re.Pattern[str], names: list[str]) -> list[str] | None:
    """Volumes the unit's `up` needs but never creates; None if it runs no `up`.

    A script that builds its `-f` list in an array (dev-up.sh) names the overlay
    and runs `up` in the same unit, which is all this needs to see.
    """
    if not (ref.search(unit) and _UP.search(unit)):
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
    bad, exempt_hit = [], set()
    for overlay, ref in GUARDED.items():
        names = _external_volume_names(overlay)
        for rel in _scanned():
            path = REPO_ROOT / rel
            if not path.is_file():
                continue
            text = path.read_text(errors="ignore")
            for heading, unit in _units(rel, text):
                missing = _missing(unit, ref, names)
                if missing is None:
                    continue
                if (rel, heading) in EXEMPT:
                    exempt_hit.add((rel, heading))
                elif missing:
                    bad.append(f"{rel} [{heading or 'whole file'}]: no `docker volume create` for {missing}")
    return bad, exempt_hit


def test_base_compose_declares_no_external_volume():
    # A fresh customer install runs the base alone; an external volume there
    # aborts its first `up` (GDXA-687 on every new box).
    assert _external_volume_names(BASE_COMPOSE) == []


def test_dev_overlay_still_declares_external_volumes():
    # Vacuity check: with no external volumes the guard below proves nothing.
    assert _external_volume_names(_DEV) == ["docker_db_data", "docker_gdx_uploads"]


def test_dev_up_is_scanned():
    # The one script that runs the dev overlay; if the scan stops seeing its
    # `up`, the guard is green over nothing.
    text = (REPO_ROOT / "gdx_dispatch/docker/dev-up.sh").read_text()
    assert _missing(text, GUARDED[_DEV], _external_volume_names(_DEV)) == []


def test_every_documented_up_creates_the_external_volumes():
    bad, _ = _findings()
    assert not bad, "\n".join(bad)


def test_every_exemption_still_matches_an_up():
    _, hit = _findings()
    assert set(EXEMPT) - hit == set(), "stale EXEMPT entries: drop them"


@pytest.mark.parametrize(
    "text, expect",
    [
        # The README Quick start as it stood before GDXA-687, on the dev overlay.
        ("docker compose -f a/docker-compose.yml -f a/docker-compose.dev.yml up -d --build", ["a", "b"]),
        ("docker volume create a\ndocker volume create b\ndocker compose -f x/docker-compose.dev.yml up -d", []),
        ("docker compose -f x/docker-compose.yml up -d", None),
        ("docker compose -f x/docker-compose.prod.yml up -d", None),
        ("plain `docker compose -f …docker-compose.dev.yml` then `up` creates a second stack", None),
    ],
)
def test_guard_can_fail(text, expect):
    assert _missing(text, GUARDED[_DEV], ["a", "b"]) == expect


def test_markdown_split_ignores_comments_inside_fences():
    md = "## One\n```bash\n# not a heading\ndocker compose -f docker-compose.yml up -d\n```\n## Two\n"
    headings = [h for h, _ in _units("x.md", md)]
    assert headings == [None, "One", "Two"]
