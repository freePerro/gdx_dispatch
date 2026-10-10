"""Guard: every copy-paste `docker run ... -m pytest` example keeps /tmp in RAM.

Tests build file-backed SQLite databases under ``tmp_path``. Without
``--tmpfs /tmp`` the container's /tmp is overlayfs on the host disk and every
commit waits on an fsync: ``run_tests_split.sh`` measured
``test_mobile_job_clock.py`` at 138 s on disk against 3.5 s on tmpfs
(2026-10-05). The script injects the flag into its own ``PYTEST``, but a
single-file run pasted from an agent file or a guide never goes through it.
GDXA-484 found 13 of the 14 ``.claude/agents/*.md`` files carrying the bare
one-liner.

What turns this red: a runnable example — an inline code span (even one
wrapped across lines), or a logical line of a fenced block (backslash
continuations joined) — containing both ``docker run`` and ``pytest`` but no
``--tmpfs``. The pre-fix agent line
``docker run --rm --entrypoint python -v "$PWD":/app ... docker-app -m pytest``
is exactly that input; ``test_the_pre_fix_line_is_flagged`` pins it.

Not examples, so not checked: a ``PYTEST=`` value handed to
``run_tests_split.sh`` (the script adds the flag), a ``#`` comment line, and
an elided mention (``docker run ... -m pytest``) in prose.

Scope is the present-tense surfaces: the agent files, CLAUDE.md, and
``gdx_dispatch/docs/``. ``docs/design/`` is the record of what was decided and
when; an archived audit's command line stays as it was run.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

SURFACES = (
    ".claude/agents/*.md",
    "CLAUDE.md",
    "gdx_dispatch/docs/**/*.md",
)

# An inline span may wrap across lines inside a paragraph (the agent files are
# hard-wrapped), so spans are matched per paragraph, not per line.
_INLINE_CODE = re.compile(r"`([^`]+)`")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _inline_spans(para: list[tuple[int, str]]) -> list[tuple[int, str]]:
    if not para:
        return []
    start, text = para[0][0], "\n".join(line for _no, line in para)
    return [
        (start + text.count("\n", 0, m.start()), " ".join(m.group(1).split()))
        for m in _INLINE_CODE.finditer(text)
    ]


def _code_fragments(text: str) -> list[tuple[int, str]]:
    """``(line_no, fragment)`` for every inline span and fenced logical line."""
    out: list[tuple[int, str]] = []
    in_fence = False
    pending: list[str] = []
    pending_start = 0
    para: list[tuple[int, str]] = []
    for no, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            out.extend(_inline_spans(para))
            para = []
            in_fence = not in_fence
            pending = []
            continue
        if in_fence:
            if not pending:
                pending_start = no
            stripped = line.rstrip()
            if stripped.endswith("\\"):
                pending.append(stripped[:-1])
                continue
            pending.append(stripped)
            logical = " ".join(p.strip() for p in pending)
            pending = []
            if not logical.startswith("#"):
                out.append((pending_start, logical))
        elif line.strip():
            para.append((no, line))
        else:
            out.extend(_inline_spans(para))
            para = []
    out.extend(_inline_spans(para))
    return out


def _is_bare_example(fragment: str) -> bool:
    # `\bpytest\b`, not `-m pytest`: `--entrypoint pytest` runs it too.
    if "docker run" not in fragment or not re.search(r"\bpytest\b", fragment):
        return False
    if "..." in fragment or "…" in fragment:
        return False  # an elided mention, not something to paste
    if re.search(r"\bPYTEST=", fragment.split("docker run", 1)[0]):
        return False  # run_tests_split.sh injects --tmpfs into its PYTEST
    return "--tmpfs" not in fragment


def find_bare_examples(text: str) -> list[tuple[int, str]]:
    return [(no, frag) for no, frag in _code_fragments(text) if _is_bare_example(frag)]


def _surface_files() -> list[Path]:
    files: set[Path] = set()
    for pattern in SURFACES:
        files.update(REPO_ROOT.glob(pattern))
    return sorted(files)


def test_surfaces_exist():
    """An empty scan passes vacuously — make sure the agent files are reachable."""
    agents = list(REPO_ROOT.glob(".claude/agents/*.md"))
    assert agents, f"no agent files under {REPO_ROOT / '.claude/agents'}"


def test_every_docker_pytest_example_mounts_tmpfs():
    findings = []
    for path in _surface_files():
        for no, frag in find_bare_examples(path.read_text(encoding="utf-8")):
            findings.append(f"{path.relative_to(REPO_ROOT)}:{no}: {frag[:120]}")
    assert not findings, (
        "docker pytest example(s) without --tmpfs; add "
        "`--tmpfs /tmp:rw,exec,size=32g` after `--rm`:\n  " + "\n  ".join(findings)
    )


def test_the_pre_fix_line_is_flagged():
    pre_fix = (
        '- Backend, targeted: `docker run --rm --entrypoint python -v "$PWD":/app -w /app '
        "-e PYTHONPATH=/app -e JWT_SECRET=x docker-app -m pytest -rs -k \"<pattern>\"`.\n"
    )
    assert len(find_bare_examples(pre_fix)) == 1
    fixed = pre_fix.replace("--rm ", "--rm --tmpfs /tmp:rw,exec,size=32g ")
    assert find_bare_examples(fixed) == []


def test_a_continued_fenced_example_is_flagged():
    block = (
        "```bash\n"
        "docker run --rm --entrypoint python -v $PWD:/app -w /app docker-app \\\n"
        "  -m pytest gdx_dispatch/tests/test_x.py -v\n"
        "```\n"
    )
    assert find_bare_examples(block) == [(2, "docker run --rm --entrypoint python -v $PWD:/app -w /app docker-app -m pytest gdx_dispatch/tests/test_x.py -v")]


def test_non_examples_are_not_flagged():
    text = (
        "```bash\n"
        'PYTEST="docker run --rm --entrypoint python \\\n'
        '  -v $PWD:/app -w /app docker-app -m pytest" \\\n'
        "  bash gdx_dispatch/tools/run_tests_split.sh\n"
        "# a single-file `docker run ... -m pytest` does NOT check\n"
        "```\n"
        "Never a bare `docker run ... -m pytest` for the matrix.\n"
    )
    assert find_bare_examples(text) == []


def test_a_wrapped_inline_span_is_flagged():
    """Reflowing the agent file's ~200-char span must not hide it (GDXA-484 audit)."""
    text = (
        "- Backend, targeted: `docker run --rm --entrypoint python -v \"$PWD\":/app\n"
        "  -w /app docker-app -m pytest -k \"<pattern>\"`.\n"
    )
    assert [no for no, _f in find_bare_examples(text)] == [1]


def test_an_entrypoint_pytest_example_is_flagged():
    text = "`docker run --rm --entrypoint pytest -v $PWD:/app -w /app docker-app -k x`\n"
    assert len(find_bare_examples(text)) == 1
