"""Guard: no NEW real email address or phone number reaches the public repo.

Companion to ``gdx_dispatch/tools/pii_literal_scan.py`` (GDXA-354). Two
halves, and the second is the one that matters:

1. ``test_no_new_pii_literals`` — the ratchet over the tracked tree.
2. The counterfactuals — a ratchet that cannot go red is decoration
   (``tests/authz_sweep.py`` is this repo's standing example). These feed the
   detector a fabricated violation and assert it would fail the ratchet.

The fabricated values below are invented for this test; the scanner exempts
this file by path so they cannot reach the baseline.
"""
from __future__ import annotations

import re

from gdx_dispatch.tools import pii_literal_scan as scan

# Invented. The failing inputs the ratchet must reject.
REAL_LOOKING_EMAIL = 'CONTACT = "gerald.fenwick@fenwickdoors.co"'
REAL_LOOKING_PHONE = 'PHONE = "612-555-8841"'
# The E.164 form Phone.com webhooks carry — how real numbers reach fixtures.
REAL_LOOKING_E164 = 'payload = {"from": "+16125558841"}'

# Entries in .pii_literal_baseline when frozen on main (GDXA-354, 2026-10-07).
# Lower this when the baseline shrinks; raising it means re-freezing over a new
# value, which is the one wrong answer — say why in the PR if you must.
BASELINE_CEILING = 147


def test_baseline_cannot_silently_grow():
    n = len(scan.load_baseline())
    assert n <= BASELINE_CEILING, (
        f".pii_literal_baseline has {n} entries, ceiling {BASELINE_CEILING}: "
        "it was re-frozen over a new value instead of fixing the value"
    )


def test_no_new_pii_literals():
    """If this fails, the output names the file and value. Is it real?

      * Real or invented -> use an ``example.com`` address or a ``555-01xx``
                            number.
      * Not an address or phone at all -> append ``pii-ok`` to the line.

    Re-freezing the baseline to make this pass is the one wrong answer.
    """
    baseline = scan.load_baseline()
    assert baseline, "baseline is empty or missing — the ratchet would accept everything"
    new = [shown for sig, shown in scan.scan_repo() if sig not in baseline]
    assert not new, (
        f"{len(new)} email/phone literal(s) not in .pii_literal_baseline:\n  "
        + "\n  ".join(new[:25])
    )


def test_ratchet_goes_red_for_real_looking_email_and_phone():
    """The end-to-end counterfactual: both fabricated values are detected AND
    absent from the frozen baseline, so the ratchet above would fail on them."""
    baseline = scan.load_baseline()
    for line, rule in ((REAL_LOOKING_EMAIL, "E1"), (REAL_LOOKING_PHONE, "T1"),
                       (REAL_LOOKING_E164, "T1")):
        hits = scan.scan_text("gdx_dispatch/core/fixture.py", line)
        assert [s for s, _ in hits if s.startswith(f"{rule}|")], f"{rule} missed {line!r}"
        assert [s for s, _ in hits if s not in baseline], f"baseline already absorbs {line!r}"


def test_reserved_email_domains_pass():
    for addr in ("office@example.com", "a@mail.example.org", "x@shop.test", "y@foo.invalid"):
        assert scan.scan_text("f.py", f'E = "{addr}"') == [], addr


def test_lookalike_of_reserved_domain_is_flagged():
    """A substring check would wave ``example.com.evil.co`` through."""
    hits = scan.scan_text("f.py", 'E = "x@example.com.fenwickdoors.co"')
    assert [s for s, _ in hits if s.startswith("E1|")]


def test_fiction_phone_block_passes():
    """NANP reserves 555-0100..555-0199 for fictional use."""
    for phone in ("612-555-0142", "(612) 555-0199", "+1 612.555.0100", "+16125550142"):
        assert scan.scan_text("f.py", f'P = "{phone}"') == [], phone


def test_phone_signature_ignores_formatting():
    """A reformatted baselined number is the same number, not a new one."""
    sigs = {
        scan.scan_text("f.py", f'P = "{p}"')[0][0]
        for p in ("612-555-8841", "(612) 555-8841", "(612)555-8841", "+1 612.555.8841",
                  "1-612-555-8841", "+16125558841")
    }
    assert len(sigs) == 1, sigs


def test_signature_is_path_independent():
    """Moving a file must not make its values read as new."""
    a = scan.scan_text("a/one.py", REAL_LOOKING_EMAIL)
    b = scan.scan_text("b/two.md", REAL_LOOKING_EMAIL)
    assert {s for s, _ in a} == {s for s, _ in b}


def test_pii_ok_escape_hatch_suppresses_the_line():
    assert scan.scan_text("f.py", REAL_LOOKING_PHONE + "  # pii-ok: a part number") == []


def test_baseline_contains_no_plaintext_values():
    """The baseline stores ``RULE|sha256[:16]``. A refactor that writes raw
    values would turn it into a tidy list of every address in the tree."""
    bad = [
        ln for ln in scan.BASELINE_PATH.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
        and not re.fullmatch(r"(?:E1|T1)\|[0-9a-f]{16}", ln.strip())
    ]
    assert not bad, f"baseline lines are not hashed E1/T1 signatures: {bad[:5]}"
