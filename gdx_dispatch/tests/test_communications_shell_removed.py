"""The Communications shell is gone and stays gone (#350).

``routers/communications.py`` was a third messaging system next to the two
that work (``/inbox`` → Outlook, ``/phone-com/messages`` → Phone.com).
Threads, messages and the do-not-contact list lived in module-level dicts in
the API process; its senders were unconfigured on prod; and
``POST /api/communications/send`` answered 201 with a green "Message sent"
toast when nothing left the building. It was deleted together with
``core/email.py`` (its only non-test importer) and the ``/communications``
screen (parallel-fake removal — issue #350, PR #549).

These are ROUTE-TABLE assertions, not status-code assertions. This app
answers 405 for a POST to any unmatched path (the SPA catch-all is GET-only
on ``/{full_path:path}``), so a path that never existed is indistinguishable
from a deleted one by status alone — #485 shipped that mistake, #486 fixed it.
"""
from __future__ import annotations

import importlib.util

import pytest

from gdx_dispatch.tests.conftest import app_route_paths

#: Every path the deleted router registered (13 routes, two APIRouters).
REMOVED_PATHS = {
    "/api/sms/send",
    "/api/sms/webhook",
    "/api/sms/conversations",
    "/api/sms/conversations/{phone}",
    "/api/inbox/unread-count",
    "/api/inbox/folders",
    "/api/email/send",
    "/api/communications/threads",
    "/api/communications/threads/{thread_id}/messages",
    "/api/communications/send",
    "/api/communications/timeline/{customer_id}",
    "/api/communications/dnc",
    "/api/communications/dnc/{customer_id}",
}

#: Nothing survives under the prefix. ``POST /api/communications/missed-call``
#: (the Twilio voice webhook, #187) outlived the #350 removal because it had a
#: working sender; that sender was Twilio, never configured on prod, and both
#: went on 2026-09-06 (see test_twilio_retired.py).
SURVIVING_PREFIX_PATHS: set[str] = set()


@pytest.fixture(scope="module")
def route_paths() -> set[str]:
    from gdx_dispatch.app import create_app

    return app_route_paths(create_app())


def test_no_removed_route_is_registered(route_paths):
    still_there = sorted(REMOVED_PATHS & route_paths)
    assert not still_there, (
        "Routes of the deleted Communications shell are registered again: "
        f"{still_there}"
    )


def test_nothing_new_under_the_communications_prefix(route_paths):
    under_prefix = {p for p in route_paths if p.startswith("/api/communications")}
    unexpected = sorted(under_prefix - SURVIVING_PREFIX_PATHS)
    assert not unexpected, (
        f"New /api/communications/* routes appeared; nothing is allowlisted: {unexpected}"
    )


@pytest.mark.parametrize(
    "module_name",
    ["gdx_dispatch.routers.communications", "gdx_dispatch.core.email"],
)
def test_deleted_modules_do_not_exist(module_name):
    assert importlib.util.find_spec(module_name) is None, (
        f"{module_name} is importable again — it was deleted in #350"
    )

