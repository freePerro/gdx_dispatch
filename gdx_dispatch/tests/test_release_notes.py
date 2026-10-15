"""In-app release notes: the bundled files, the parser, the router, the
installed-version resolution and the release tool (draft / check / body /
changes) that the Release workflow runs."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from shutil import which

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.core import app_version
from gdx_dispatch.routers import release_notes as rn_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.services import release_notes as svc
from gdx_dispatch.services.release_notes import (
    NOTES_DIR,
    ReleaseNotesError,
    load_releases,
    parse_release_file,
)
from gdx_dispatch.tools import release_notes as tool


def _write(directory: Path, version: str, body: str = "## Highlights\n- One thing\n", **meta: str) -> Path:
    fields = {"version": version, "date": "2026-10-12", "title": "", **meta}
    head = "".join(f"{k}: {v}\n" for k, v in fields.items())
    path = directory / f"v{version}.md"
    path.write_text(f"---\n{head}---\n{body}", encoding="utf-8")
    return path


# ── the bundled files ──────────────────────────────────────────────────────


def test_every_bundled_notes_file_parses():
    releases = load_releases(NOTES_DIR)
    assert len(releases) >= 215
    versions = [r.version for r in releases]
    assert len(versions) == len(set(versions))
    assert [r.key for r in releases] == sorted((r.key for r in releases), reverse=True)


def test_the_hand_written_backfill_content_survived():
    by_version = {r.version: r for r in load_releases(NOTES_DIR)}
    assert "Superseded by v1.41.1" in by_version["1.41.0"].warning
    assert by_version["1.41.0"].title
    assert any(s["title"] == "Highlights" for s in by_version["1.2.0"].sections)


def test_the_newest_release_has_finished_notes(monkeypatch):
    """The newest notes file is the one the next tag ships; it must pass ``check``.

    Its format, that is. Whether it is current depends on what merged after
    it, which every PR changes; the Release workflow judges that at the tag,
    and the git tests below pin how.
    """
    monkeypatch.setattr(tool, "merged_after_notes", lambda repo, path, ref: ([], "pinned"))
    newest = load_releases(NOTES_DIR)[0].version
    assert tool.main(["check", newest]) == 0


# ── parser ─────────────────────────────────────────────────────────────────


def test_parser_builds_sections_headings_paragraphs_and_lists(tmp_path):
    body = (
        "Intro paragraph\nwrapped onto two lines.\n\n"
        "## Highlights\n### Office\n- **Bold** item with a [link](https://example.com)\n"
        "  continued on the next line\n* star bullet\n\n---\n\n## Notes\n`code` text\n"
    )
    r = parse_release_file(_write(tmp_path, "2.0.0", body, title="A **title**", warning="Careful"))
    assert r.title == "A title"
    assert r.warning == "Careful"
    assert r.sections == [
        {"title": "", "blocks": [{"kind": "paragraph", "text": "Intro paragraph wrapped onto two lines."}]},
        {"title": "Highlights", "blocks": [
            {"kind": "heading", "text": "Office"},
            {"kind": "list", "items": ["Bold item with a link continued on the next line", "star bullet"]},
        ]},
        {"title": "Notes", "blocks": [{"kind": "paragraph", "text": "code text"}]},
    ]


def test_parser_keeps_html_as_text_for_the_page_to_escape(tmp_path):
    r = parse_release_file(_write(tmp_path, "2.0.0", "- <script>alert(1)</script>\n"))
    assert r.sections[0]["blocks"][0]["items"] == ["<script>alert(1)</script>"]


@pytest.mark.parametrize(
    ("name", "text", "message"),
    [
        ("v2.0.0.md", "version: 2.0.0\n", "must start with"),
        ("v2.0.0.md", "---\nversion: 2.0.0\ndate: 2026-10-12\n", "never closed"),
        ("v2.0.0.md", "---\nversion: 2.0.0\ndate: 2026-10-12\nauthor: x\n---\n", "unknown frontmatter"),
        ("v2.0.0.md", "---\nversion: 2.0.1\ndate: 2026-10-12\n---\n", "does not match"),
        ("v2.0.0.md", "---\nversion: 2.0.0\ndate: Oct 12\n---\n", "YYYY-MM-DD"),
        ("v2.0.0.md", "---\nversion: 2.0.0\ndate: 2026-10-12\n---\n# Big\n", "headings"),
        ("v2.0.0.md", "---\nversion: 2.0.0\ndate: 2026-10-12\n---\n#### Small\n", "headings"),
        ("notes.md", "---\nversion: 2.0.0\ndate: 2026-10-12\n---\n", "vX.Y.Z.md"),
    ],
)
def test_parser_refuses_files_outside_the_format(tmp_path, name, text, message):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ReleaseNotesError, match=message):
        parse_release_file(path)


def test_load_releases_merges_the_generated_change_lists(tmp_path):
    _write(tmp_path, "1.0.0")
    _write(tmp_path, "1.10.0")
    _write(tmp_path, "1.9.0")
    (tmp_path / "changes.json").write_text(json.dumps({"releases": {"1.10.0": ["fix (#2)"]}}))
    releases = load_releases(tmp_path)
    assert [r.version for r in releases] == ["1.10.0", "1.9.0", "1.0.0"]
    assert releases[0].changes == ["fix (#2)"]
    assert releases[1].changes == []


# ── installed version ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("app", "image", "installed", "display"),
    [
        ("1.145.0", "1.146.0", "1.145.0", "1.145.0"),   # an operator pin wins
        ("v1.145.0", "", "1.145.0", "1.145.0"),
        ("latest", "1.146.0", "1.146.0", "1.146.0"),    # :latest installs know their release
        ("latest", "", None, "latest"),
        ("dev", "", None, "dev"),
        ("", "", None, "dev"),
        ("abc1234", "", None, "abc1234"),
    ],
)
def test_installed_version_resolution(monkeypatch, app, image, installed, display):
    monkeypatch.setenv("APP_VERSION", app)
    monkeypatch.setenv("GDX_IMAGE_VERSION", image)
    assert app_version.installed_version() == installed
    assert app_version.display_version() == display


# ── router ─────────────────────────────────────────────────────────────────


def _client(monkeypatch, tmp_path, *, signed_in: bool = True) -> TestClient:
    _write(tmp_path, "1.0.0")
    _write(tmp_path, "1.1.0", title="Second")
    monkeypatch.setattr(rn_router, "bundled_releases", lambda: tuple(load_releases(tmp_path)))
    monkeypatch.setenv("APP_VERSION", "1.1.0")
    app = FastAPI()
    app.include_router(rn_router.router)
    if signed_in:
        app.dependency_overrides[get_current_user] = lambda: {"user_id": "u", "role": "technician"}
    return TestClient(app)


def test_router_lists_every_release_with_the_installed_version(monkeypatch, tmp_path):
    body = _client(monkeypatch, tmp_path).get("/api/release-notes").json()
    assert body["installed_version"] == "1.1.0"
    assert [r["version"] for r in body["releases"]] == ["1.1.0", "1.0.0"]
    assert body["releases"][0]["title"] == "Second"


def test_router_returns_one_release_or_404(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    assert client.get("/api/release-notes/1.0.0").json()["version"] == "1.0.0"
    assert client.get("/api/release-notes/v1.0.0").json()["version"] == "1.0.0"
    assert client.get("/api/release-notes/9.9.9").status_code == 404
    assert client.get("/api/release-notes/latest").status_code == 404


def test_router_needs_a_signed_in_user(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, signed_in=False)
    assert client.get("/api/release-notes").status_code == 401
    assert client.get("/api/release-notes/1.0.0").status_code == 401


def test_the_app_mounts_the_router():
    from gdx_dispatch.app import app
    from gdx_dispatch.tests.conftest import app_route_paths

    paths = app_route_paths(app)
    assert "/api/release-notes" in paths
    assert "/api/release-notes/{version}" in paths


# ── the release tool ───────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
             "PATH": "/usr/bin:/bin:/usr/local/bin"},
    ).stdout


def _commit(repo: Path, message: str) -> None:
    _git(repo, "commit", "--allow-empty", "-m", message)


# The docker-app image has no git, so this skips in the local matrix; CI
# runs pytest on the runner, where git is installed, and the Release
# workflow runs the same code there.
@pytest.mark.skipif(which("git") is None, reason="git not on PATH")
def test_changes_are_first_parent_commits_between_notes_versions(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "initial")
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "fix(a): first (#1)")
    _git(repo, "tag", "v1.0.1")          # tagged, never got a notes file
    _commit(repo, "Merge pull request #2 from someone/branch\n\nAdd the second thing")
    _commit(repo, "Merge branch 'main' into feature")
    _commit(repo, "feat(b): third (#3)")
    _git(repo, "tag", "v1.1.0")

    changes = tool.compute_changes(repo, ["1.1.0", "1.0.0", "1.2.0"])
    # 1.0.1 has no notes file, so its change folds into 1.1.0; 1.2.0 has no tag yet.
    assert changes == {"1.1.0": ["feat(b): third (#3)", "Add the second thing (#2)", "fix(a): first (#1)"]}


def test_check_refuses_a_missing_or_unfilled_notes_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(tool, "NOTES_DIR", tmp_path)
    _write(tmp_path, "1.0.0")
    assert tool.main(["check", "v1.1.0"]) == 1
    assert "missing" in capsys.readouterr().err
    _write(tmp_path, "1.1.0", "## Highlights\n- \n")
    assert tool.main(["check", "v1.1.0"]) == 1
    assert "draft skeleton" in capsys.readouterr().err
    # A title alone is not enough: the skeleton's blank bullet still refuses.
    _write(tmp_path, "1.1.0", "## Highlights\n- \n", title="Finished")
    assert tool.main(["check", "v1.1.0"]) == 1
    assert "draft skeleton" in capsys.readouterr().err
    _write(tmp_path, "1.1.0", "## Highlights\n- Done\n", title="Finished")
    assert tool.main(["check", "v1.1.0"]) == 0
    assert tool.main(["check", "nonsense"]) == 2


def test_check_fails_when_any_file_does_not_parse(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(tool, "NOTES_DIR", tmp_path)
    _write(tmp_path, "1.1.0", "- Done\n", title="Finished")
    (tmp_path / "v1.0.0.md").write_text("no frontmatter\n")
    assert tool.main(["check", "1.1.0"]) == 1
    assert "do not parse" in capsys.readouterr().err


def test_draft_writes_a_skeleton_that_check_refuses(monkeypatch, tmp_path):
    monkeypatch.setattr(tool, "NOTES_DIR", tmp_path)
    monkeypatch.setattr(tool, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(tool, "change_lines", lambda repo, rng: ["fix: x (#9)"])
    _write(tmp_path, "1.0.0")
    assert tool.main(["draft", "1.1.0"]) == 0
    assert parse_release_file(tmp_path / "v1.1.0.md").version == "1.1.0"
    assert tool.main(["check", "1.1.0"]) == 1
    before = (tmp_path / "v1.1.0.md").read_text()
    assert tool.main(["draft", "1.1.0"]) == 0          # never overwrites
    assert (tmp_path / "v1.1.0.md").read_text() == before


def _notes_repo(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """A repo whose v1.1.0 notes were written, then a fix and a doc-only change merged after."""
    repo = tmp_path / "repo"
    notes = repo / "notes"
    notes.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _write(notes, "1.0.0")
    _git(repo, "add", ".")
    _commit(repo, "initial")
    _git(repo, "tag", "v1.0.0")
    _write(notes, "1.1.0", "## Highlights\n- Done\n", title="Finished")
    _git(repo, "add", ".")
    _commit(repo, "docs(release-notes): v1.1.0 notes (#5)")
    (repo / "app.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _commit(repo, "fix(app): merged after the notes (#6)")
    (repo / "my docs.md").write_text("docs\n")    # a space in the path is still a doc
    _git(repo, "add", ".")
    _commit(repo, "docs: a doc-only change (#7)")
    monkeypatch.setattr(tool, "NOTES_DIR", notes)
    monkeypatch.setattr(tool, "_repo_root", lambda: repo)
    return repo, notes


@pytest.mark.skipif(which("git") is None, reason="git not on PATH")
def test_draft_on_an_existing_file_lists_what_merged_after_its_last_edit(monkeypatch, tmp_path, capsys):
    repo, notes = _notes_repo(tmp_path, monkeypatch)
    before = (notes / "v1.1.0.md").read_text()
    assert tool.main(["draft", "1.1.0", "--ref", "HEAD"]) == 0
    out = capsys.readouterr().out
    since_release, _, since_edit = out.partition("was last edited")
    assert "(#5)" in since_release and "(#6)" in since_release
    assert "(#6)" in since_edit
    assert "(#5)" not in since_edit and "(#7)" not in since_edit   # the edit itself, and docs only
    assert (notes / "v1.1.0.md").read_text() == before
    # Editing the file re-anchors it: nothing is newer any more.
    _write(notes, "1.1.0", "## Highlights\n- Done\n- And the fix\n", title="Finished")
    _git(repo, "commit", "-qam", "docs(release-notes): fold in #6 (#8)")
    assert tool.main(["draft", "1.1.0", "--ref", "HEAD"]) == 0
    assert "Nothing but docs merged since v1.1.0.md" in capsys.readouterr().out


@pytest.mark.skipif(which("git") is None, reason="git not on PATH")
def test_check_refuses_notes_older_than_a_later_merge(monkeypatch, tmp_path, capsys):
    repo, notes = _notes_repo(tmp_path, monkeypatch)
    assert tool.main(["check", "1.1.0", "--ref", "HEAD"]) == 1
    err = capsys.readouterr().err
    assert "fix(app): merged after the notes (#6)" in err
    assert "(#7)" not in err
    _write(notes, "1.1.0", "## Highlights\n- Done\n- And the fix\n", title="Finished")
    _git(repo, "commit", "-qam", "docs(release-notes): fold in #6 (#8)")
    assert tool.main(["check", "1.1.0", "--ref", "HEAD"]) == 0
    # Once tagged, the file is judged at its tag: the next release's merges
    # are not held against it, unless a ref is asked for.
    _git(repo, "tag", "v1.1.0")
    (repo / "app.py").write_text("x = 2\n")
    _git(repo, "commit", "-qam", "fix(app): for the next release (#9)")
    assert tool.main(["check", "1.1.0"]) == 0
    assert tool.main(["check", "1.1.0", "--ref", "HEAD"]) == 1
    assert "(#9)" in capsys.readouterr().err


@pytest.mark.skipif(which("git") is None, reason="git not on PATH")
def test_check_fails_on_a_ref_git_cannot_resolve(monkeypatch, tmp_path, capsys):
    """A full clone that cannot answer is a fault, not a pass: a typo'd ref, or
    the default origin/main in a repo with no such remote."""
    _notes_repo(tmp_path, monkeypatch)
    assert tool.main(["check", "1.1.0", "--ref", "orign/main"]) == 1
    assert "orign/main is not a commit here" in capsys.readouterr().err
    assert tool.main(["check", "1.1.0"]) == 1
    assert "origin/main is not a commit here" in capsys.readouterr().err


@pytest.mark.skipif(which("git") is None, reason="git not on PATH")
def test_check_only_warns_where_git_cannot_tell(monkeypatch, tmp_path, capsys):
    repo, _ = _notes_repo(tmp_path, monkeypatch)
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow))
    monkeypatch.setattr(tool, "NOTES_DIR", shallow / "notes")
    monkeypatch.setattr(tool, "_repo_root", lambda: shallow)
    assert tool.main(["check", "1.1.0"]) == 0
    assert "shallow clone" in capsys.readouterr().err
    # A notes file not committed yet (drafting it locally) is not judged either.
    monkeypatch.setattr(tool, "NOTES_DIR", repo / "notes")
    monkeypatch.setattr(tool, "_repo_root", lambda: repo)
    _write(repo / "notes", "1.2.0", "## Highlights\n- New\n", title="Next")
    assert tool.main(["check", "1.2.0", "--ref", "HEAD"]) == 0
    assert "not committed at HEAD" in capsys.readouterr().err


def test_body_is_the_file_after_its_frontmatter_with_the_warning_first(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(tool, "NOTES_DIR", tmp_path)
    _write(tmp_path, "1.1.0", "## Highlights\n- Done\n\n---\n\n- after a rule\n",
           title="Finished", warning="Do not deploy")
    assert tool.main(["body", "1.1.0", "--title"]) == 0
    assert capsys.readouterr().out == "v1.1.0 — Finished\n"
    assert tool.main(["body", "v1.1.0"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("> **Warning:** Do not deploy\n\n## Highlights\n- Done\n")
    assert "version:" not in out
    assert "- after a rule" in out


def test_tool_imports_nothing_beyond_the_standard_library():
    """The Release workflow runs the tool on a bare runner before any pip install."""
    code = (
        "import sys; import gdx_dispatch.tools.release_notes; "
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'fastapi','sqlalchemy','pydantic','starlette','httpx'}); print(bad)"
    )
    out = subprocess.run(
        ["python3", "-c", code], check=True, capture_output=True, text=True,
        cwd=Path(svc.__file__).resolve().parents[2],
    ).stdout.strip()
    assert out == "[]"
