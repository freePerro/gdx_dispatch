"""Resale quotes are private to the reseller (modules/reseller).

A contractor's markup and their customers' names are theirs: no staff screen,
report, export or AI tool reads them. The guard is structural — the tables
and models may be named only in the files that serve the reseller their own
data. A new reader anywhere else turns this red and has to argue its case
here, in review, rather than arriving quietly.

What it cannot see: a generic reader that walks every table by reflection
without naming it (the customer merge does exactly that to move rows; it reads
nothing back out).
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
NAMES = re.compile(
    r"reseller_profiles|resale_quotes|ResellerProfile|ResaleQuote|modules[./]reseller"
    # `from gdx_dispatch.modules import reseller` names none of the above.
    r"|modules\s+import\s+[^\n]*\breseller\b"
)

ALLOWED = {
    "modules/reseller/models.py",
    "modules/reseller/service.py",
    "routers/portal_resale.py",
    # /portal/context: three booleans for the reseller's own tabs.
    "routers/portal.py",
    # Model registration for create_all.
    "models/__init__.py",
    "migrations/versions/113_reseller_quotes.py",
    # Comments naming the module the helper serves; no data access.
    "core/branding_logo.py",
    "core/pdf_generator.py",
    "templates/resale_quote_pdf.html",
    "tools/agent_ownership.txt",
}
SUFFIXES = {".py", ".html", ".vue", ".js", ".ts", ".txt", ".sql"}
SKIP_DIRS = {"tests", "node_modules", "dist", "__pycache__"}


def _offenders(root: pathlib.Path = ROOT) -> list[str]:
    found = []
    for path in root.rglob("*"):
        if path.suffix not in SUFFIXES or not path.is_file():
            continue
        rel = path.relative_to(root)
        if SKIP_DIRS & set(rel.parts):
            continue
        if rel.as_posix() in ALLOWED:
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if NAMES.search(body):
            found.append(rel.as_posix())
    return sorted(found)


def test_only_the_reseller_surface_names_the_reseller_tables():
    assert _offenders() == []


def test_the_allowlist_has_no_dead_entries():
    """An entry for a file that no longer names the tables is a hole someone
    could later fill without review."""
    stale = [
        rel for rel in sorted(ALLOWED)
        if not (ROOT / rel).is_file() or not NAMES.search((ROOT / rel).read_text(encoding="utf-8"))
    ]
    assert stale == []


def test_the_guard_can_fail(tmp_path):
    """Falsifier: a staff router that imports the model, and a report that
    reads the table by raw SQL, are both caught."""
    (tmp_path / "routers").mkdir()
    (tmp_path / "routers" / "reports.py").write_text("from gdx_dispatch.modules.reseller.models import ResaleQuote\n")
    (tmp_path / "routers" / "portal_resale.py").write_text("ResaleQuote\n")  # allowed
    (tmp_path / "routers" / "staff.py").write_text(
        "from gdx_dispatch.modules import proposals, reseller\nreseller.service.get_profile\n"
    )
    (tmp_path / "export.sql").write_text("SELECT markup_pct FROM resale_quotes;\n")
    assert _offenders(tmp_path) == ["export.sql", "routers/reports.py", "routers/staff.py"]
