"""Guard: unexpected-exception text must not reach client-visible detail.

The 2026-09-20 sweep (silent-500 class, continuation of #751/#757) found and
fixed 22 handlers that interpolated raw exception text into an HTTP `detail`
— either from a broad ``except Exception`` (whatever blew up, the caller saw
it) or on a 500 (infrastructure internals to the browser, often with no
server-side log). The rule this pins:

- A **typed domain exception** re-raised as a 4xx with its authored message
  is GOOD practice (DepositError, PeriodLockedError, ValueError from our own
  validators...) and stays out of scope here.
- A **broad except or a 500** must send an opaque detail; the cause belongs
  in ``log.exception``, not the response body.

Scope is tracked non-test Python under routers/, modules/, core/, api/.
Each allowlisted site carries the reason it was reviewed out.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("gdx_dispatch/routers", "gdx_dispatch/modules", "gdx_dispatch/core", "gdx_dispatch/api")

# detail=f"...{exc}..." / detail=str(exc) / {"detail": f"...{exc}"} / {"detail": str(exc)}
# — exact-variable match (exc|e|err) so handler literals like {entity} never trip it.
_DETAIL = re.compile(
    r'(?:detail=|"detail":\s*)'
    r'(?:f"[^"]*\{(?:exc|e|err)(?:[.!:][^}]*)?\}[^"]*"|str\((?:exc|e|err)\))'
)
_EXCEPT = re.compile(r"^\s*except\s+(.+?)(?:\s+as\s+\w+)?\s*:")
_STATUS = re.compile(r"status_code=(\d+)|,\s*(\d{3})\)\s*$")

# Reviewed 2026-09-20 — each is a typed/authored message or an admin-input
# echo with no infrastructure error reachable. Keyed (path, except-clause)
# with an EXACT expected count, so a NEW leak in an allowlisted file still
# reddens the gate, and a removed site reddens it as stale (prune here).
ALLOWED: dict[tuple[str, str], int] = {
    # validate_fi_host raises ValueError/OutboundURLBlocked with authored text
    # (the inline noqa comment documents it); broad spelling only. Two call
    # sites (register + update institution).
    ("gdx_dispatch/modules/bank_feeds/router.py", "Exception"): 2,
    # render_preview is pure string formatting over the admin's own template;
    # the error IS the message the admin needs to fix it.
    ("gdx_dispatch/modules/numbering/router.py", "Exception"): 1,
    # broad except, but str(exc) is emitted only AFTER isinstance() narrows to
    # ExpenseCompositionError / PeriodLockedError (authored 409 messages) —
    # one site per branch; everything else falls through to a bare `raise`.
    ("gdx_dispatch/routers/vendor_invoices.py", "Exception"): 2,
    # MCP tool invocation: the consumer is the owner's AI assistant, which
    # needs the exception type+text to self-correct (REPL semantics); the
    # same detail lands in the audit row, so the server keeps a full trace.
    ("gdx_dispatch/core/mcp_invoke.py", "Exception"): 1,
}


def _broad_or_500_leaks() -> tuple[list[str], dict[tuple[str, str], int]]:
    hits: list[str] = []
    allowed_seen: dict[tuple[str, str], int] = dict.fromkeys(ALLOWED, 0)
    for base in SCAN_DIRS:
        for f in (REPO / base).rglob("*.py"):
            rel = f.relative_to(REPO).as_posix()
            if "test" in f.name:
                continue
            lines = f.read_text().splitlines()
            for i, line in enumerate(lines):
                if not _DETAIL.search(line):
                    continue
                # Status may sit on the detail line itself (the
                # jsonable_response(..., 500) spelling — end-anchored, so it
                # must be matched against the BARE line) or on the previous
                # line (HTTPException(status_code=500, on its own line).
                sm = _STATUS.search(line) or _STATUS.search(lines[i - 1] if i else "")
                status = next((g for g in (sm.groups() if sm else ()) if g), "?")
                clause = ""
                for j in range(i, max(i - 20, -1), -1):
                    em = _EXCEPT.match(lines[j])
                    if em:
                        clause = em.group(1).strip()
                        break
                broad = clause in ("Exception", "BaseException")
                if not (broad or status == "500"):
                    continue
                key = (rel, clause)
                if key in allowed_seen:
                    allowed_seen[key] += 1
                    if allowed_seen[key] <= ALLOWED[key]:
                        continue
                hits.append(f"{rel}:{i + 1} status={status} except {clause}")
    return hits, allowed_seen


def test_no_unexpected_exception_text_in_client_detail() -> None:
    hits, allowed_seen = _broad_or_500_leaks()
    assert not hits, (
        "client-visible detail interpolates raw exception text from a broad "
        "except or on a 500 — log the cause, send an opaque detail:\n  "
        + "\n  ".join(hits)
    )
    stale = {k: (seen, ALLOWED[k]) for k, seen in allowed_seen.items() if seen < ALLOWED[k]}
    assert not stale, (
        f"stale allowlist entries (site count fell below the allowance — prune them): {stale}"
    )
