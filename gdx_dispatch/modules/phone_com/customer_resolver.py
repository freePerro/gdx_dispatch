import logging
import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.pii import HashColumn
from gdx_dispatch.models.tenant_models import Customer

log = logging.getLogger(__name__)

try:
    import phonenumbers
    HAS_PHONENUMBERS = True
except ImportError:
    HAS_PHONENUMBERS = False

def normalize_e164(raw: str | None, *, default_country: str = "US") -> str | None:
    """
    Strips formatting, prepends country code if missing, returns +1XXXXXXXXXX shape for US.
    Returns None on un-parseable input.
    """
    if not raw:
        return None

    raw = raw.strip()
    if not raw:
        return None

    if HAS_PHONENUMBERS:
        try:
            parsed = phonenumbers.parse(raw, default_country)
            if phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
            return None
        except phonenumbers.NumberParseException as exc:
            log.debug("phone_com.normalize_e164_unparseable raw=%r err=%s", raw, exc)
            return None
    else:
        # Simple regex fallback: extract digits
        digits = re.sub(r"\D", "", raw)
        if not digits:
            return None

        # If 10 digits, assume US and prepend +1
        if len(digits) == 10:
            return f"+1{digits}"
        # If 11 digits and starts with 1, assume US
        if len(digits) == 11 and digits.startswith("1"):
            return f"+{digits}"

        if raw.startswith("+") and re.match(r"^\+\d+$", raw):
            return raw

        return None

def phone_hash(e164: str) -> str:
    """sha256 hex digest of the E.164 string. Matches gdx_dispatch.core.pii.HashColumn."""
    return HashColumn.hash_for_search(e164)

def match_phone_to_customer(tenant_db: Session, e164: str | None) -> Customer | None:
    """normalizes, hashes, queries Customer.phone_hash == hash (LIMIT 1)."""
    if not e164:
        return None

    norm_e164 = normalize_e164(e164)
    if not norm_e164:
        return None

    h = phone_hash(norm_e164)
    stmt = select(Customer).where(Customer.phone_hash == h).limit(1)
    return tenant_db.execute(stmt).scalar_one_or_none()

def match_caller_id(tenant_db: Session, caller_id: str | None) -> UUID | None:
    """convenience wrapper returning just the customer UUID."""
    customer = match_phone_to_customer(tenant_db, caller_id)
    return customer.id if customer else None
