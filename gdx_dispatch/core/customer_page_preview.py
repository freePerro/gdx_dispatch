"""Let staff see the page on the other side of a texted link before it goes.

The two customer pages a text can carry — ``/pay/{token}`` (an invoice) and
``/proposals/{token}`` (an estimate) — are built for the customer, and opening
them as staff goes wrong three ways:

* a draft is 404 on both (they only resolve once the document went out), and a
  text is usually composed FROM a draft;
* every human open is audited as ``*_viewed_by_customer`` (core/customer_views.py),
  so the office checking the page would read back as "the customer opened it";
* the pages are live: the pay page charges, the proposal page accepts.

A preview link is the public URL plus ``?preview=<signature>``. The signature
names the document kind, the document id and an expiry, keyed off the app's
signing secret. It is deliberately NOT a JWT: several routes decode any JWT
signed with that key without checking its type, so a JWT here would be a new
token one of them might accept as a login. This string is not a JWT and no
decoder in the app will ever take it.

The public routes honour a valid signature by skipping the "not sent yet" gate
and the view record, and render the page with every action switched off. An
invalid or expired signature is refused outright: it is never treated as a
plain customer visit, because whoever holds it is staff, not the customer.

Minted only inside the authenticated ``sms-preview`` routes, which already
decided the caller may see that document.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Literal
from uuid import UUID

Kind = Literal["invoice", "estimate"]

#: Long enough for someone to edit a text, glance at the page and come back;
#: short enough that a link copied out of the dialog stops working the same
#: afternoon.
PREVIEW_TTL_SECONDS = 30 * 60

_DOMAIN = b"gdx-customer-page-preview-v1"


def _key() -> bytes:
    # Derived, not reused: the login key never signs anything but logins.
    from gdx_dispatch.routers.auth.core import SIGN_KEY

    return hmac.new(SIGN_KEY.encode("utf-8"), _DOMAIN, hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str) -> str:
    return _b64(hmac.new(_key(), payload.encode("ascii"), hashlib.sha256).digest())


def mint(kind: Kind, doc_id: UUID | str, *, now: float | None = None) -> str:
    expires = int((now if now is not None else time.time()) + PREVIEW_TTL_SECONDS)
    payload = f"{kind}:{UUID(str(doc_id))}:{expires}"
    return f"{_b64(payload.encode('ascii'))}.{_sign(payload)}"


def verify(token: str | None, kind: Kind, doc_id: UUID | str, *, now: float | None = None) -> bool:
    """True only for an unexpired signature minted for exactly this document."""
    if not token or len(token) > 256 or token.count(".") != 1:
        return False
    body, sig = token.split(".")
    try:
        payload = _unb64(body).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return False
    if not hmac.compare_digest(sig, _sign(payload)):
        return False
    parts = payload.split(":")
    if len(parts) != 3:
        return False
    got_kind, got_id, expires = parts
    try:
        same_doc = UUID(got_id) == UUID(str(doc_id))
        live = int(expires) > (now if now is not None else time.time())
    except ValueError:
        return False
    return got_kind == kind and same_doc and live


def preview_url(kind: Kind, doc_id: UUID | str, public_token: str | None) -> str | None:
    """Same-origin path to the customer page in preview mode, or None when the
    document has no public token. Relative on purpose: staff open it from the
    app, whose origin may differ from GDX_PUBLIC_BASE_URL."""
    if not public_token:
        return None
    page = "pay" if kind == "invoice" else "proposals"
    return f"/{page}/{public_token}?preview={mint(kind, doc_id)}"
