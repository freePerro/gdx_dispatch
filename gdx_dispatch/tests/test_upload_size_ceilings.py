"""No handler may pull an upload body into memory before it has a ceiling.

Until 2026-08-26 seven handlers had no ceiling at all; the only bound was nginx
`client_max_body_size`, 50M on the prod vhost. Several others had one written
*after* the read — `data = await file.read()` then `if len(data) > cap` — which
bounds what gets STORED but not what gets ALLOCATED. The body is already
resident by the time that check runs, so it does not prevent the thing the
ceiling exists to prevent.

The scanner asserts the ABSENCE of unbounded-read-before-ceiling. Absence is
the property that can regress; asserting a guard IS present would only prove
someone typed it.

WHAT COUNTS AS A CEILING — behaviour, deliberately not a keyword:
  * a call to assert_upload_within_limit BEFORE the first unbounded read, or
  * a bounded read — `file.read(cap + 1)` — which never over-allocates.

An earlier version of this file counted "the function mentions 413 anywhere"
as a ceiling. That blessed the exact read-then-check pattern this module
exists to remove, and one stray integer defeated it. It also matched only a
parameter literally named `file`. Both mistakes are pinned by
`test_the_scanner_catches_every_evasion_we_could_think_of` below — the point
of which is that a counterfactual proving the scanner can fail for ONE shape
proves almost nothing about the shapes it lets through.
"""
from __future__ import annotations

import ast
import io
import pathlib

import pytest
from fastapi import HTTPException, UploadFile

from gdx_dispatch.core.upload_limits import MAX_UPLOAD_BYTES, assert_upload_within_limit

_PKG = pathlib.Path(__file__).resolve().parents[1]
#: Both trees — an upload route outside routers/ is still an upload route.
DEFAULT_ROOTS = (_PKG / "routers", _PKG / "modules")
GUARD_NAMES = {"assert_upload_within_limit", "_assert_upload_within_limit"}


def _upload_param_names(fn: ast.AST) -> set[str]:
    """Params typed UploadFile / list[UploadFile], whatever they are called."""
    a = fn.args
    return {
        arg.arg
        for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs]
        if arg.annotation is not None and "UploadFile" in ast.unparse(arg.annotation)
    }


def _first_unguarded_read(fn: ast.AST) -> int | None:
    names = _upload_param_names(fn)
    if not names:
        return None
    # `for upload in files:` — the loop variable is an upload too.
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.For)
            and isinstance(node.iter, ast.Name)
            and node.iter.id in names
            and isinstance(node.target, ast.Name)
        ):
            names.add(node.target.id)

    unbounded, guards = [], []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name in GUARD_NAMES:
            guards.append(node.lineno)
        if isinstance(func, ast.Attribute) and func.attr == "read":
            root = func.value
            while isinstance(root, ast.Attribute):
                root = root.value
            if getattr(root, "id", None) in names and not node.args:
                unbounded.append(node.lineno)

    if not unbounded:
        return None
    if guards and min(guards) < min(unbounded):
        return None
    return min(unbounded)


def unguarded_upload_readers(roots=DEFAULT_ROOTS) -> list[str]:
    out = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:  # pragma: no cover
                continue
            for fn in ast.walk(tree):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    line = _first_unguarded_read(fn)
                    if line:
                        try:
                            shown = path.relative_to(_PKG)
                        except ValueError:  # a temp tree from the counterfactuals
                            shown = path.name
                        out.append(f"{shown}::{fn.name}:{line}")
    return out


def test_no_handler_reads_an_upload_before_it_has_a_ceiling():
    unguarded = unguarded_upload_readers()
    assert unguarded == [], (
        "these read an upload body with no ceiling in front of it: "
        + ", ".join(unguarded)
        + " — call assert_upload_within_limit(file, cap) BEFORE the read, or read(cap + 1)"
    )


# ── the counterfactual, done properly ────────────────────────────────────────

EVASIONS = {
    "plain unbounded read":        "async def f(file: UploadFile):\n    d = await file.read()\n",
    "413 present but after read":  "async def f(file: UploadFile):\n    d = await file.read()\n    if len(d) > 9:\n        raise HTTPException(status_code=413)\n",
    "stray 413 in a list":         "async def f(file: UploadFile):\n    codes = [429, 413, 503]\n    d = await file.read()\n",
    "stray 413 in a dict":         "async def f(file: UploadFile):\n    m = {413: 'too big'}\n    d = await file.read()\n",
    "param not named file":        "async def f(upload: UploadFile):\n    d = await upload.read()\n",
    "param named logo":            "async def f(logo: UploadFile):\n    d = await logo.read()\n",
    "list of uploads in a loop":   "async def f(files: list[UploadFile]):\n    for u in files:\n        d = await u.read()\n",
    "read via .file attribute":    "async def f(file: UploadFile):\n    d = file.file.read()\n",
    "guard AFTER the read":        "async def f(file: UploadFile):\n    d = await file.read()\n    assert_upload_within_limit(file)\n",
}

ACCEPTABLE = {
    "guard before the read":       "async def f(file: UploadFile):\n    assert_upload_within_limit(file)\n    d = await file.read()\n",
    "bounded read":               "async def f(file: UploadFile):\n    d = await file.read(101)\n",
    "bounded read on .file":      "async def f(file: UploadFile):\n    d = file.file.read(101)\n",
    "not an upload at all":       "async def f(body: bytes):\n    d = body\n",
}


@pytest.mark.parametrize("label", sorted(EVASIONS))
def test_the_scanner_catches_every_evasion_we_could_think_of(tmp_path, label):
    """A scanner proven to fail for one input proves one input.

    The previous version of this test wrote a single leaky function and
    declared the ratchet counterfactually sound. Seven of the nine shapes
    below slipped past it.
    """
    root = tmp_path / "r"
    root.mkdir()
    (root / "m.py").write_text(EVASIONS[label])
    assert unguarded_upload_readers(roots=(root,)), f"scanner MISSED: {label}"


@pytest.mark.parametrize("label", sorted(ACCEPTABLE))
def test_the_scanner_does_not_cry_wolf(tmp_path, label):
    """Equally important. A scanner that flags correct code gets muted, and
    then it is worth nothing on the day it is right."""
    root = tmp_path / "r"
    root.mkdir()
    (root / "m.py").write_text(ACCEPTABLE[label])
    assert unguarded_upload_readers(roots=(root,)) == [], f"false positive: {label}"


# ── the guard itself ─────────────────────────────────────────────────────────


def _sized(data: bytes) -> UploadFile:
    return UploadFile(io.BytesIO(data), size=len(data), filename="b.bin")


def test_guard_refuses_over_the_ceiling():
    with pytest.raises(HTTPException) as exc:
        assert_upload_within_limit(_sized(b"\0" * (MAX_UPLOAD_BYTES + 1)))
    assert exc.value.status_code == 413


def test_guard_allows_exactly_the_ceiling():
    """Pin the boundary from both sides — a guard that refused everything would
    satisfy the test above while breaking every upload in the app."""
    assert_upload_within_limit(_sized(b"\0" * MAX_UPLOAD_BYTES))  # must not raise


def test_guard_allows_an_unmeasurable_upload_rather_than_rejecting_blind():
    """Failing closed on our own uncertainty would turn an unreadable size into
    an outage. The read still bounds what gets stored."""

    class _Opaque:
        size = None
        file = object()  # no tell/seek -> AttributeError path

    assert_upload_within_limit(_Opaque())  # must not raise


# ── raw-body ceiling (the JSON branch of admin_ops.import_customers) ─────────


class _Req:
    def __init__(self, headers):
        self.headers = headers


def test_body_guard_refuses_an_oversized_declared_length():
    from gdx_dispatch.core.upload_limits import assert_body_within_limit

    with pytest.raises(HTTPException) as exc:
        assert_body_within_limit(_Req({"content-length": str(MAX_UPLOAD_BYTES + 1)}))
    assert exc.value.status_code == 413


def test_body_guard_allows_the_ceiling_and_anything_unstated():
    """Content-Length is client-supplied and absent on chunked requests. Falling
    through is deliberate — this is a courtesy ceiling, not a boundary. nginx
    client_max_body_size is the only thing that refuses bytes before arrival."""
    from gdx_dispatch.core.upload_limits import assert_body_within_limit

    assert_body_within_limit(_Req({"content-length": str(MAX_UPLOAD_BYTES)}))
    assert_body_within_limit(_Req({}))                      # chunked / unstated
    assert_body_within_limit(_Req({"content-length": "abc"}))  # unparseable
