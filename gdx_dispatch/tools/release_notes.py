"""Release-notes helper — the release steps that touch ``gdx_dispatch/release_notes/``.

    python -m gdx_dispatch.tools.release_notes draft 1.146.0
        Write release_notes/v1.146.0.md (title + an empty Highlights) and print
        the PRs merged since the last release, to write the highlights from.
        When the file already exists, also print what merged after its last
        edit, and leave the file alone.

    python -m gdx_dispatch.tools.release_notes check 1.146.0
        Exit non-zero unless v1.146.0.md exists, every notes file parses, and
        no change other than docs merged after the file was last edited
        (a warning, not a failure, where git cannot tell, as in a shallow
        clone). The Release workflow runs this before it builds anything.

    python -m gdx_dispatch.tools.release_notes body 1.146.0 [--title]
        Print the notes file as the GitHub Release body (or its title line).
        The Release workflow uses it for the published Release.

    python -m gdx_dispatch.tools.release_notes changes --write
        Compute each release's merged-change list from git and write
        release_notes/changes.json. The Release workflow runs this before the
        image build; the file is git-ignored.

A release's changes are the first-parent commits between the previous
release that has a notes file and this one. A tag with no notes file (v1.132.0
was tagged and never published) folds into the next release, as GitHub's
own generated notes do.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

from gdx_dispatch.core.app_version import parse_semver
from gdx_dispatch.services.release_notes import (
    CHANGES_FILE,
    NOTES_DIR,
    ReleaseNotesError,
    load_releases,
    parse_release_file,
)

_MERGE_RE = re.compile(r"^Merge pull request #(\d+) from \S+")
_SKIP_RE = re.compile(r"^Merge (branch|remote-tracking branch) ")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def _tag_exists(repo: Path, tag: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"],
        capture_output=True,
    ).returncode == 0


def _change_line(subject: str, body: str) -> str | None:
    """A commit as a change line: an old-style merge commit becomes its PR
    title from the body; a branch merge is None."""
    subject = subject.strip()
    merge = _MERGE_RE.match(subject)
    if merge:
        title = next((b.strip() for b in body.splitlines() if b.strip()), "")
        return f"{title} (#{merge[1]})" if title else subject
    if _SKIP_RE.match(subject):
        return None
    return subject


def change_lines(repo: Path, rev_range: str) -> list[str]:
    """One line per first-parent commit in ``rev_range``, oldest last.

    A squash merge's subject already reads ``title (#N)``; an old-style merge
    commit is replaced by its PR title from the commit body.
    """
    out = _git(repo, "log", "--first-parent", "--format=%s%x1f%b%x1e", rev_range)
    lines: list[str] = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        subject, _, body = record.partition("\x1f")
        line = _change_line(subject, body)
        if line is not None:
            lines.append(line)
    return lines


def merged_after_notes(repo: Path, path: Path, ref: str) -> tuple[list[str] | None, str]:
    """The changes at ``ref`` that landed after the last commit touching ``path``.

    A notes file drafted days before its release says nothing about what
    merged after it (v1.146.0.md, drafted in #1090 and caught up by hand in
    #1159). Returns ``(lines, anchor)``: the first-parent commits after the
    file's last edit, oldest last, leaving out commits that change only
    ``.md`` files, and the short hash of that edit. ``lines`` is None when
    git cannot answer (no git, not a repository, a shallow clone, the file
    not committed at ``ref``), and ``anchor`` then says why. Anything else
    git refuses, a ref that does not resolve among them, raises
    ``ReleaseNotesError``: in the Release workflow's full clone that is a
    fault, not a reason to wave the tag through.

    It proves the file was edited after the last code change, not that the
    edit covers it.
    """
    try:
        shallow = _git(repo, "rev-parse", "--is-shallow-repository").strip()
    except OSError:
        return None, "git is not installed here"
    except subprocess.CalledProcessError:
        return None, f"{repo} is not a git repository"
    if shallow == "true":
        return None, "this is a shallow clone, so the history after the file is incomplete"
    try:
        _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except subprocess.CalledProcessError:
        raise ReleaseNotesError(f"{ref} is not a commit here") from None
    try:
        rel = path.resolve().relative_to(repo.resolve()).as_posix()
        anchor = _git(repo, "log", "-1", "--first-parent", "--format=%H", ref, "--", rel).strip()
        if not anchor:
            return None, f"{rel} is not committed at {ref} yet"
        lines: list[str] = []
        for sha in _git(repo, "rev-list", "--first-parent", f"{anchor}..{ref}").split():
            files = _git(repo, "diff", "-z", "--name-only", f"{sha}^1", sha).split("\0")
            if all(f.endswith(".md") for f in files if f):
                continue
            subject, _, body = _git(repo, "log", "-1", "--format=%s%x1f%b", sha).partition("\x1f")
            line = _change_line(subject, body)
            if line is not None:
                lines.append(line)
    except (ValueError, subprocess.CalledProcessError) as exc:
        raise ReleaseNotesError(f"git could not read the history after {path.name}: {exc}") from None
    return lines, anchor[:12]


def compute_changes(repo: Path, versions: list[str]) -> dict[str, list[str]]:
    """``{version: [change, ...]}`` for each version whose tag and previous release tag exist."""
    ordered = sorted(versions, key=lambda v: parse_semver(v) or (0, 0, 0))
    out: dict[str, list[str]] = {}
    for prev, cur in zip(ordered, ordered[1:], strict=False):
        if _tag_exists(repo, f"v{prev}") and _tag_exists(repo, f"v{cur}"):
            out[cur] = change_lines(repo, f"v{prev}..v{cur}")
    return out


def _repo_root() -> Path:
    return Path(_git(NOTES_DIR, "rev-parse", "--show-toplevel").strip())


def _repo_root_or(fallback: Path) -> Path:
    """The repo root, or ``fallback`` where there is no git to ask."""
    try:
        return _repo_root()
    except (OSError, subprocess.CalledProcessError):
        return fallback


def _arg_version(args: argparse.Namespace) -> str | None:
    """``args.version`` as ``X.Y.Z``, or None (after saying why) when it is not one."""
    parts = parse_semver(args.version)
    if parts is None:
        print(f"not a version: {args.version}", file=sys.stderr)
        return None
    return ".".join(map(str, parts))


def cmd_changes(args: argparse.Namespace) -> int:
    versions = [r.version for r in load_releases(NOTES_DIR)]
    changes = compute_changes(_repo_root(), versions)
    payload = {"releases": changes}
    if args.write:
        (NOTES_DIR / CHANGES_FILE).write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {NOTES_DIR / CHANGES_FILE}: {len(changes)} releases")
    else:
        json.dump(payload, sys.stdout, indent=1)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    version = _arg_version(args)
    if version is None:
        return 2
    try:
        versions = {r.version for r in load_releases(NOTES_DIR)}
    except ReleaseNotesError as exc:
        print(f"release notes do not parse: {exc}", file=sys.stderr)
        return 1
    if version not in versions:
        print(
            f"missing gdx_dispatch/release_notes/v{version}.md — merge it to main before "
            f"tagging (python -m gdx_dispatch.tools.release_notes draft {version})",
            file=sys.stderr,
        )
        return 1
    release = next(r for r in load_releases(NOTES_DIR) if r.version == version)
    blank = [
        item for s in release.sections for b in s["blocks"] if b["kind"] == "list"
        for item in b["items"] if not item
    ]
    if not release.title or not release.sections or blank:
        print(
            f"v{version}.md is still the draft skeleton — give it a title and fill in "
            "the highlights (an empty '- ' bullet counts as unfilled)",
            file=sys.stderr,
        )
        return 1
    # Judge the file at its own tag once it is cut, so a later run does not
    # count the next release's merges against it.
    repo = _repo_root_or(NOTES_DIR)
    ref = args.ref
    if ref is None:
        try:
            ref = f"v{version}" if _tag_exists(repo, f"v{version}") else "origin/main"
        except OSError:
            ref = "origin/main"
    try:
        newer, anchor = merged_after_notes(repo, NOTES_DIR / f"v{version}.md", ref)
    except ReleaseNotesError as exc:
        print(exc, file=sys.stderr)
        return 1
    if newer is None:
        print(f"warning: cannot tell whether v{version}.md is current at {ref}: {anchor}", file=sys.stderr)
    elif newer:
        print(
            f"v{version}.md was last edited in {anchor}, and {len(newer)} change(s) merged after it "
            f"at {ref}. Fold in what users will notice, re-confirm the warning: line for any new "
            "migration, and merge the edited file (which re-anchors it):\n"
            + "".join(f"  {line}\n" for line in newer),
            file=sys.stderr,
        )
        return 1
    print(f"v{version}.md present; {len(versions)} notes files parse")
    return 0


def github_body(path: Path) -> str:
    """The notes file as the GitHub Release body: the warning, then everything after the frontmatter."""
    release = parse_release_file(path)
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    body = text.split("\n---\n", 1)[1].strip() if "\n---\n" in text else ""
    if release.warning:
        body = f"> **Warning:** {release.warning}\n\n{body}".strip()
    return body + "\n"


def cmd_body(args: argparse.Namespace) -> int:
    version = _arg_version(args)
    if version is None:
        return 2
    path = NOTES_DIR / f"v{version}.md"
    release = parse_release_file(path)
    if args.title:
        print(f"v{release.version}" + (f" — {release.title}" if release.title else ""))
    else:
        sys.stdout.write(github_body(path))
    return 0


def cmd_draft(args: argparse.Namespace) -> int:
    version = _arg_version(args)
    if version is None:
        return 2
    path = NOTES_DIR / f"v{version}.md"
    previous = [r.version for r in load_releases(NOTES_DIR) if r.key < parse_semver(version)]
    repo = _repo_root()
    if previous:
        print(f"Merged since v{previous[0]} (write the highlights from these):\n")
        for line in change_lines(repo, f"v{previous[0]}..{args.ref}"):
            print(f"  {line}")
        print()
    if path.exists():
        try:
            newer, anchor = merged_after_notes(repo, path, args.ref)
        except ReleaseNotesError as exc:
            print(exc, file=sys.stderr)
            return 1
        if newer is None:
            print(f"Cannot tell what merged after {path.name} was last edited: {anchor}\n")
        elif newer:
            print(
                f"Merged since {path.name} was last edited ({anchor}) — fold these in, and "
                "re-confirm the warning: line for any new migration:\n"
            )
            for line in newer:
                print(f"  {line}")
            print()
        else:
            print(f"Nothing but docs merged since {path.name} was last edited ({anchor}).\n")
        print(f"{path} already exists; not overwriting")
        return 0
    path.write_text(
        f"---\nversion: {version}\ndate: {dt.date.today().isoformat()}\ntitle: \n---\n"
        "## Highlights\n- \n",
        encoding="utf-8",
    )
    print(f"wrote {path} — fill in the title and highlights")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("draft", help="write a notes file for a new release")
    p.add_argument("version")
    p.add_argument("--ref", default="origin/main", help="what the release will tag (default origin/main)")
    p.set_defaults(func=cmd_draft)
    p = sub.add_parser("check", help="fail unless the release has a current notes file and all files parse")
    p.add_argument("version")
    p.add_argument(
        "--ref", default=None,
        help="what the release tags (default: the vX.Y.Z tag if it exists, else origin/main)",
    )
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("body", help="print a notes file as the GitHub Release body (or --title)")
    p.add_argument("version")
    p.add_argument("--title", action="store_true", help="print the release title line instead")
    p.set_defaults(func=cmd_body)
    p = sub.add_parser("changes", help="compute per-release change lists from git")
    p.add_argument("--write", action="store_true", help=f"write release_notes/{CHANGES_FILE}")
    p.set_defaults(func=cmd_changes)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
