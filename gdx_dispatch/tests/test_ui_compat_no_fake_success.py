"""The ui_compat shims must never fake a successful write.

Context (docs/design/frontend-contract-gaps-2026-08-12.md, class C6): these
handlers used to answer a bare ``{"ok": True}`` to mutations that touched
nothing. The Vue read that as success, popped a toast, closed the dialog, and
the user's edit vanished.

The three formerly-shadowed handlers — ``PATCH /api/onboarding/checklist`` and
campaign activate/deactivate — were removed on 2026-09-01 after their canonical
handlers were verified live. The route-shadow regression gate now protects
against reintroducing their competing registrations.
"""
from __future__ import annotations

from gdx_dispatch.routers import ui_compat


def test_the_fake_success_helper_is_gone():
    """`_ok()` returned a bare {"ok": True} and was the vehicle for the whole
    class. It has no callers left; keep it that way.

    An absence assertion, deliberately: proving a symbol is *gone* is real
    evidence, unlike asserting some string is present in the source.
    """
    assert not hasattr(ui_compat, "_ok"), (
        "ui_compat._ok is back. If a handler has nothing to do, refuse loudly "
        "via _not_implemented rather than returning a success literal."
    )
