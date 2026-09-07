"""Absence guards for the Twilio removal (2026-09-06).

Twilio was the SMS provider this app was written against and never configured
on this install: the integration flag was false, no credential row existed,
no ``TWILIO_*`` variable reached a container, and ``inbound_sms`` held 0 rows.
Phone.com is the SMS and voice system (``modules/phone_com/``). The removal is
recorded in the design record for the Twilio removal (2026-09-06).

Every guard here names its counterfactual — the change that turns it red —
because a check that cannot fail is decoration.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PKG = REPO / "gdx_dispatch"

#: Counterfactual: restoring either module makes ``find_spec`` return a spec.
REMOVED_MODULES = ["gdx_dispatch.core.sms", "gdx_dispatch.core.twilio_signature"]

#: Counterfactual: re-registering any of these on any router makes the app's
#: route table contain it again.
REMOVED_PATHS = {
    "/api/inbound-sms",
    "/api/inbound-sms/{sms_id}",
    "/api/inbound-sms/{sms_id}/link",
    "/api/inbound-sms/webhook",
    "/api/communications/missed-call",
    "/api/jobs/{job_id}/on-my-way",
}

_ENV_READ = re.compile(r"TWILIO_[A-Z_]+")
_IMPORT = re.compile(r"^\s*(?:import|from)\s+twilio\b", re.M)


@pytest.mark.parametrize("module_name", REMOVED_MODULES)
def test_removed_modules_are_not_importable(module_name):
    assert importlib.util.find_spec(module_name) is None, f"{module_name} is importable again"


def test_removed_routes_are_not_registered():
    from gdx_dispatch.app import create_app
    from gdx_dispatch.tests.conftest import app_route_paths

    back = sorted(REMOVED_PATHS & app_route_paths(create_app()))
    assert not back, f"Twilio-era routes are registered again: {back}"


def test_inbound_sms_model_and_table_are_gone():
    """Counterfactual: re-adding ``class InboundSMS`` to tenant_models registers
    the table on the metadata and puts the attribute back."""
    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.models import tenant_models

    assert not hasattr(tenant_models, "InboundSMS")
    assert "inbound_sms" not in TenantBase.metadata.tables


#: The package plus the repo-root helpers that import from it. Tests,
#: migrations, the frontend tree and the git-ignored local demo stack are out.
_SCAN_ROOTS = [PKG, REPO / "tools", REPO / "scripts", REPO / "conftest.py"]
_SKIP = ("/tests/", "/migrations/", "/frontend/", "node_modules", "/docker/demo/")


def _source_files():
    for root in _SCAN_ROOTS:
        if not root.exists():
            continue
        for p in ([root] if root.is_file() else root.rglob("*.py")):
            rel = p.relative_to(REPO).as_posix()
            if any(s in f"/{rel}" for s in _SKIP):
                continue
            yield p


def test_no_twilio_env_read_or_import_in_source():
    """Counterfactual: a new ``os.getenv("TWILIO_AUTH_TOKEN")`` or
    ``import twilio`` under gdx_dispatch/, tools/, scripts/ or conftest.py
    (tests, migrations, the frontend tree and the local demo stack excluded)
    is reported by file. Proven to fail on a planted file during the diff
    audit of 2026-09-06."""
    hits = []
    for p in _source_files():
        text = p.read_text(encoding="utf-8", errors="replace")
        if _ENV_READ.search(text) or _IMPORT.search(text):
            hits.append(p.relative_to(REPO).as_posix())
    assert not hits, f"Twilio references in source: {hits}"


def test_source_scan_can_go_red():
    """The scan above must be able to fail — prove the patterns match the
    shapes they are meant to catch."""
    assert _ENV_READ.search('os.getenv("TWILIO_AUTH_TOKEN")')
    assert _IMPORT.search("from twilio.rest import Client")
    assert _IMPORT.search("import twilio")
    assert not _IMPORT.search("import twiliofoo")
