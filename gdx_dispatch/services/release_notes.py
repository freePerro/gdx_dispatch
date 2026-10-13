"""Release notes shipped inside the image — the in-app "What's new" history.

Source: ``gdx_dispatch/release_notes/vX.Y.Z.md``, one file per release, the
human-written part (title, highlights, warnings). Format and the release
step that writes them: ``gdx_dispatch/release_notes/README.md``.

The per-release list of merged changes is NOT hand-kept. The Release
workflow computes it from git at build time
(``python -m gdx_dispatch.tools.release_notes changes --write``) into
``release_notes/changes.json``, which is git-ignored and exists only inside a
release image. A dev build has no change lists, only the written notes.

The body is parsed into plain-text blocks — headings, paragraphs and bullet
lists — so the page renders text, never HTML. A file outside the format
raises ``ReleaseNotesError``; ``tests/test_release_notes.py`` parses every
bundled file, so a bad file fails CI rather than a page.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from gdx_dispatch.core.app_version import parse_semver

NOTES_DIR = Path(__file__).resolve().parent.parent / "release_notes"
CHANGES_FILE = "changes.json"

_FILENAME_RE = re.compile(r"^v(\d+\.\d+\.\d+)\.md$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FRONTMATTER_KEYS = {"version", "date", "title", "warning"}
# A bare "-" is an empty bullet (the draft skeleton's "- " after rstrip).
_BULLET_RE = re.compile(r"^[-*](?: +(.*))?$")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")


class ReleaseNotesError(ValueError):
    """A notes file that does not follow the format."""


@dataclass
class Release:
    version: str
    date: str
    title: str
    warning: str = ""
    sections: list[dict[str, Any]] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[int, int, int]:
        parts = parse_semver(self.version)
        assert parts is not None
        return parts

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "date": self.date,
            "title": self.title,
            "warning": self.warning,
            "sections": self.sections,
            "changes": self.changes,
        }


def plain_text(text: str) -> str:
    """Strip the inline markdown a few old release bodies used, leaving the words."""
    text = _LINK_RE.sub(r"\1", text)
    for mark in ("**", "__", "`"):
        text = text.replace(mark, "")
    return text.strip()


def _split_frontmatter(name: str, text: str) -> tuple[dict[str, str], list[str]]:
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        raise ReleaseNotesError(f"{name}: must start with a '---' frontmatter block")
    meta: dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, lines[i + 1:]
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep or key not in _FRONTMATTER_KEYS:
            raise ReleaseNotesError(f"{name}:{i + 1}: unknown frontmatter line {line!r}")
        meta[key] = value.strip()
    raise ReleaseNotesError(f"{name}: frontmatter block is never closed with '---'")


def _parse_body(name: str, lines: list[str]) -> list[dict[str, Any]]:
    """Body → ``[{title, blocks: [{kind: heading|paragraph|list, ...}]}]``.

    ``## `` opens a section, ``### `` is a heading inside it, ``- ``/``* ``
    lines form a list (an indented line continues the item above), any
    other run of lines is a paragraph. A ``---`` rule is dropped.
    """
    sections: list[dict[str, Any]] = []
    para: list[str] = []

    def current() -> dict[str, Any]:
        if not sections:
            sections.append({"title": "", "blocks": []})
        return sections[-1]

    def flush_para() -> None:
        if para:
            current()["blocks"].append({"kind": "paragraph", "text": plain_text(" ".join(para))})
            para.clear()

    for n, raw in enumerate(lines, start=1):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped == "---":
            flush_para()
            continue
        if line.startswith("#### ") or line.startswith("# "):
            raise ReleaseNotesError(f"{name}: body line {n}: only '##' and '###' headings are allowed")
        if line.startswith("## "):
            flush_para()
            sections.append({"title": plain_text(line[3:]), "blocks": []})
            continue
        if line.startswith("### "):
            flush_para()
            current()["blocks"].append({"kind": "heading", "text": plain_text(line[4:])})
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            flush_para()
            blocks = current()["blocks"]
            if not blocks or blocks[-1]["kind"] != "list":
                blocks.append({"kind": "list", "items": []})
            blocks[-1]["items"].append(plain_text(bullet[1] or ""))
            continue
        blocks = current()["blocks"]
        if raw[:1] in (" ", "\t") and not para and blocks and blocks[-1]["kind"] == "list":
            items = blocks[-1]["items"]
            items[-1] = f"{items[-1]} {plain_text(stripped)}"
            continue
        para.append(stripped)
    flush_para()
    return [s for s in sections if s["blocks"]]


def parse_release_file(path: Path) -> Release:
    m = _FILENAME_RE.match(path.name)
    if not m:
        raise ReleaseNotesError(f"{path.name}: file name must be vX.Y.Z.md")
    meta, body = _split_frontmatter(path.name, path.read_text(encoding="utf-8"))
    version = meta.get("version", "")
    if version != m[1]:
        raise ReleaseNotesError(f"{path.name}: frontmatter version {version!r} does not match the file name")
    date = meta.get("date", "")
    if not _DATE_RE.match(date):
        raise ReleaseNotesError(f"{path.name}: date must be YYYY-MM-DD, got {date!r}")
    return Release(
        version=version,
        date=date,
        title=plain_text(meta.get("title", "")),
        warning=plain_text(meta.get("warning", "")),
        sections=_parse_body(path.name, body),
    )


def load_releases(directory: Path = NOTES_DIR) -> list[Release]:
    """Every bundled release, newest first, with its change list when the build made one."""
    releases = [parse_release_file(p) for p in directory.glob("v*.md")]
    changes_path = directory / CHANGES_FILE
    if changes_path.exists():
        changes = json.loads(changes_path.read_text(encoding="utf-8")).get("releases", {})
        for r in releases:
            r.changes = [str(c) for c in changes.get(r.version, [])]
    releases.sort(key=lambda r: r.key, reverse=True)
    return releases


@lru_cache(maxsize=1)
def bundled_releases() -> tuple[Release, ...]:
    """``load_releases()`` for this image — files never change inside a running image."""
    return tuple(load_releases())
