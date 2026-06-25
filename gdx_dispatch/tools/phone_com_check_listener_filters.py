"""Diagnostic: does our Phone.com listener actually filter on tags Phone.com
recognizes?

We register listener event-filters with values like ``phone.call`` /
``phone.voicemail`` (client.DEFAULT_LISTENER_EVENT_TYPES). Phone.com's
documented event tags are bare — ``call``, ``completed-call``,
``call-recording``, ``api-error-trusted`` (no ``phone.`` prefix). If the
values don't match, the edge filter is a silent no-op: either we receive
*everything* (no real filtering) or *nothing* (and lean entirely on the
15-minute polling backstop). This also gates whether the api-error health
webhook and any future call-recording / screen-pop events ever fire.

Run inside the app container (needs DATABASE_URL + GDX_FERNET_KEY + the
stored token):

    docker exec <app-container> python -m gdx_dispatch.tools.phone_com_check_listener_filters

Read-only — lists listeners + their filters, prints a verdict. Touches nothing.
"""
from __future__ import annotations

from uuid import UUID

from gdx_dispatch.core.database import SessionLocal
from gdx_dispatch.core.tenant import single_tenant
from gdx_dispatch.models.tenant_models import AppSettings
from gdx_dispatch.modules.phone_com import key_storage
from gdx_dispatch.modules.phone_com.client import PhoneComAPIError, PhoneComClient


def main() -> int:
    tenant_id = UUID(str(single_tenant()["id"]))
    db = SessionLocal()
    try:
        token = key_storage.get_token(db, tenant_id)
        if not token:
            print("FAIL: no Phone.com token stored — integration not configured.")
            return 2
        app = db.query(AppSettings).first()
        voip_raw = app.phone_com_voip_id if app else None
        if not voip_raw:
            print("FAIL: no voip_id in AppSettings — re-run /test on the integration.")
            return 2
        voip_id = int(voip_raw)
    finally:
        db.close()

    ours = list(PhoneComClient.DEFAULT_LISTENER_EVENT_TYPES)
    print(f"voip_id={voip_id}")
    print(f"our registered filter values (client.DEFAULT_LISTENER_EVENT_TYPES): {ours}")
    print("Phone.com documented tags (events-integration-doc): call, completed-call, "
          "*ms-message, voicemail-recording, fax, call-recording, api-error-trusted\n")

    client = PhoneComClient(token=token, voip_id=voip_id)
    try:
        listeners = (client.list_listeners() or {}).get("items", [])
    except PhoneComAPIError as exc:
        print(f"FAIL: list_listeners errored: {exc}")
        return 2

    if not listeners:
        print("WARN: no listeners registered at Phone.com — webhooks are NOT wired; "
              "we are running on polling only.")
        return 1

    found_values: set[str] = set()
    for li in listeners:
        lid = li.get("id")
        cb = li.get("callback_id")
        print(f"listener id={lid} callback_id={cb}")
        try:
            filters = (client.list_listener_filters(listener_id=lid) or {}).get("items", [])
        except PhoneComAPIError as exc:
            print(f"  (could not list filters: {exc})")
            continue
        if not filters:
            print("  NO FILTERS — this listener receives ALL events (no edge filtering).")
            continue
        for f in filters:
            field = f.get("field")
            value = f.get("value")
            print(f"  filter field={field!r} operator={f.get('operator')!r} value={value!r}")
            for v in (value if isinstance(value, list) else [value]):
                if v is not None:
                    found_values.add(str(v))

    # Verdict.
    print()
    if not found_values:
        print("VERDICT: no event-value filters registered. Either we receive everything "
              "or the listener filters were never created. Our DEFAULT_LISTENER_EVENT_TYPES "
              "are therefore NOT being applied at the edge.")
        return 1
    prefixed = {v for v in found_values if v.startswith("phone.")}
    if prefixed == found_values:
        print(f"VERDICT: filters use our 'phone.*' values {sorted(found_values)}. "
              "If Phone.com expects bare tags (call, *ms-message, ...), these match "
              "NOTHING and no events are delivered — confirm by checking whether "
              "webhooks have ever arrived (phone_com rows created without a sync run).")
        return 1
    print(f"VERDICT: registered filter values at Phone.com: {sorted(found_values)}. "
          "Compare against the documented tags above — if they're bare tags, our "
          "client.DEFAULT_LISTENER_EVENT_TYPES ('phone.*') is wrong and should be "
          "updated to match.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
