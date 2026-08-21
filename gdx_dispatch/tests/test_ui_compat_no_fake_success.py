"""The ui_compat shims must never fake a successful write.

Context (docs/design/frontend-contract-gaps-2026-08-12.md, class C6): these
handlers used to answer a bare ``{"ok": True}`` to mutations that touched
nothing. The Vue read that as success, popped a toast, closed the dialog, and
the user's edit vanished.

Most of that class was converted to a logged 501 in an earlier pass. Three
survived — ``PATCH /api/onboarding/checklist`` and campaign
activate/deactivate — and were converted on 2026-08-21.

**Why these three still matter even though they are shadowed.** At runtime the
real handlers win: ``routers/onboarding.py::patch_checklist`` and
``routers/campaigns.py`` serve those paths (verified live — a PUT to a
non-existent campaign returns 404 "Campaign not found", and a PATCH to the
checklist 422s demanding ``step``/``completed``, neither of which the shims
could produce). But ``app.py`` wraps every router import in ``try/except`` and
substitutes an **empty APIRouter** on failure. If ``onboarding.py`` or
``campaigns.py`` ever fails to import, these shims become the live handlers —
and a fake success on a degraded system is precisely the failure this class
exists to prevent.

So the guard is behavioural, not a source-text grep: call the shim handlers
directly and prove they refuse.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from gdx_dispatch.routers import ui_compat


class _State:
    tenant = {"id": "t-1"}


class _Req:
    """Minimal stand-in for the Request the loud-refusal path logs from.

    `_not_implemented` reads `request.state.tenant` (via `_tenant_id`) and the
    method/url for its WARNING line, so the stub carries exactly those.
    """

    def __init__(self) -> None:
        self.method = "PATCH"
        self.url = "http://testserver/api/onboarding/checklist"
        self.headers: dict[str, str] = {}
        self.client = None
        self.state = _State()


def _user() -> dict[str, str]:
    return {"sub": "u-1", "user_id": "u-1", "role": "admin", "tenant_id": "t-1"}


@pytest.mark.parametrize(
    ("call", "label"),
    [
        (
            lambda: ui_compat.update_onboarding_checklist(
                payload=ui_compat._GenericPayload(), request=_Req(), user=_user()
            ),
            "onboarding checklist PATCH",
        ),
        (
            lambda: ui_compat.activate_campaign(
                campaign_id="c-1", request=_Req(), user=_user()
            ),
            "campaign activate",
        ),
        (
            lambda: ui_compat.deactivate_campaign(
                campaign_id="c-1", request=_Req(), user=_user()
            ),
            "campaign deactivate",
        ),
    ],
)
def test_shim_refuses_loudly_instead_of_faking_success(call, label):
    """A 501 is the contract. A dict — any dict — is the bug."""
    with pytest.raises(HTTPException) as exc:
        result = call()
        pytest.fail(
            f"{label} returned {result!r} instead of refusing. A ui_compat "
            "mutation shim that returns a value tells the frontend the write "
            "landed when nothing was written."
        )
    assert exc.value.status_code == 501, (
        f"{label} refused with {exc.value.status_code}, expected 501"
    )


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
