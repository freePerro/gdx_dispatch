"""The signed staff-preview link to a customer page (core/customer_page_preview.py).

The signature is the only thing that lets a draft's pay or proposal page render
and keeps a staff look from being recorded as the customer opening it, so these
pin what it must refuse: another document, another kind, an expired one, a
tampered one, and anything shaped like a login token.
"""
from __future__ import annotations

import time
import uuid

import jwt
import pytest

from gdx_dispatch.core import customer_page_preview as cpp

DOC = uuid.uuid4()


def test_a_fresh_signature_verifies_for_its_own_document():
    assert cpp.verify(cpp.mint("invoice", DOC), "invoice", DOC)
    assert cpp.verify(cpp.mint("estimate", str(DOC)), "estimate", DOC)


def test_it_is_bound_to_the_document():
    assert not cpp.verify(cpp.mint("invoice", DOC), "invoice", uuid.uuid4())


def test_it_is_bound_to_the_kind():
    # An invoice and an estimate never share an id in practice; the kind is
    # still signed so one page can never honour the other page's link.
    assert not cpp.verify(cpp.mint("invoice", DOC), "estimate", DOC)


def test_it_expires():
    now = time.time()
    tok = cpp.mint("invoice", DOC, now=now)
    assert cpp.verify(tok, "invoice", DOC, now=now + cpp.PREVIEW_TTL_SECONDS - 1)
    assert not cpp.verify(tok, "invoice", DOC, now=now + cpp.PREVIEW_TTL_SECONDS + 1)


def test_an_edited_expiry_breaks_the_signature():
    body, sig = cpp.mint("invoice", DOC).split(".")
    payload = cpp._unb64(body).decode()
    kind, doc, _exp = payload.split(":")
    forged = cpp._b64(f"{kind}:{doc}:{int(time.time()) + 10**9}".encode())
    assert not cpp.verify(f"{forged}.{sig}", "invoice", DOC)


@pytest.mark.parametrize(
    "junk",
    ["", "x", "a.b", "a.b.c", "." * 3, "%%%.%%%", "x" * 300, None],
)
def test_junk_is_refused_not_raised(junk):
    assert cpp.verify(junk, "invoice", DOC) is False


def test_a_login_jwt_is_not_a_preview_signature():
    from gdx_dispatch.routers.auth.core import ALG, SIGN_KEY

    login = jwt.encode({"sub": "u1", "typ": "access", "exp": int(time.time()) + 600}, SIGN_KEY, algorithm=ALG)
    assert not cpp.verify(login, "invoice", DOC)


def test_a_preview_signature_is_not_a_jwt():
    """Several routes decode any JWT signed with the login key without checking
    its type; a preview signature must never be one they could accept."""
    from gdx_dispatch.routers.auth.core import ALG, VERIFY_KEY

    with pytest.raises(jwt.InvalidTokenError):
        jwt.decode(cpp.mint("invoice", DOC), VERIFY_KEY, algorithms=[ALG])


def test_preview_url_points_at_the_customer_page():
    assert cpp.preview_url("invoice", DOC, "tok123").startswith("/pay/tok123?preview=")
    assert cpp.preview_url("estimate", DOC, "tok456").startswith("/proposals/tok456?preview=")
    assert cpp.preview_url("invoice", DOC, None) is None
