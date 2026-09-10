"""Shared-secret auth for plugin-host's ``/internal/*`` routes.

The plugin-host runs third-party plugin code and exposes a small internal API
the core app calls over the compose network: event/schedule dispatch, a restart
hook, and — the reason this module exists — a WebSocket that streams a headless
browser and **autofills the operator's stored login** (ADR-014).

That surface shipped gated on a shared secret *only when the secret happened to
be set*::

    token = os.getenv("GDX_INTERNAL_TOKEN", "")
    if token and not hmac.compare_digest(...):   # <- fail OPEN when unset

``GDX_INTERNAL_TOKEN`` was never set in production (verified on ``gdx-app-1``
and ``gdx-plugin-host-1``, 2026-09-09, image 1.118.6), so neither check had ever
run: anything that could reach the plugin-host on the compose network could open
the credential-autofill socket with no token at all. The staged rollout the old
comment described ("Sprint 3 mints the token AND isolates n8n's network") never
completed, and a staged rollout that never lands is just a hole. Issue #596.

Scope of the live exposure, stated accurately rather than dramatically: prod's
``gdx_default`` network holds only GDX services plus nginx and runs **no** n8n
container at all (``docker network inspect``, 2026-09-10), and in the customer
stack n8n sits on the ``automation`` network while plugin-host is on
``backend``. So the untrusted-workflow container the original comment worried
about does not currently share a network with plugin-host anywhere. This closes
a latent hole and a defence-in-depth gap — the value is that the gate now works
at all if any of that topology ever changes, not that something was walking
through it yesterday.

Policy is deliberately the same as ``core.inbound_email_auth`` — the module that
closed this identical shape on the inbound-mail webhook — rather than a second
invention:

* Enforcement is **off** only for a closed set of known dev/test environment
  names, so a fresh clone and the test suite work with nothing configured.
* Enforcement is **on** for anything else, *including an unrecognised value*
  like ``prod-eu``. The retired Twilio gate used the opposite shape — an
  allowlist of prod-like names — and any value nobody thought to list silently
  disabled the check.
* Enforcement is also on whenever a token is configured at all: setting one is
  an explicit request to have it checked, whatever ``GDX_ENV`` says.
* When enforcement is on and no token can be obtained, callers are **refused**.
  An absent secret is a misconfiguration, not permission to skip the check.

The token itself is **derived from SECRET_KEY** when no explicit
``GDX_INTERNAL_TOKEN`` is set (see ``token()``). That is what lets this ship
without an outage: neither live stack can be handed a new variable by a pull
request — prod mints no secrets, and the public demo's compose file is not in
git — so a fix that *required* one would have broken the plugin surface on both
the moment the image rolled.

Delivery note (issue #598, item 1): the compose files enumerate ``environment:``
explicitly and there is **no** ``env_file:``, so setting this in the operator's
environment file alone delivers nothing. ``GDX_INTERNAL_TOKEN`` is therefore
declared in the shared ``x-app-env`` anchor, which reaches app, plugin-host
**and the celery services** — ``celery-high`` runs ``deliver_plugin_event_task``
and calls ``/internal/events`` itself, so a token delivered only to app +
plugin-host would fail closed on every plugin event delivery. That passthrough
is what makes an explicit override possible; the derivation is what makes the
default work.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os

log = logging.getLogger(__name__)

# noqa S105: these are the *names* of the header and env var, not a secret.
INTERNAL_TOKEN_HEADER = "X-GDX-Internal-Token"  # noqa: S105
TOKEN_ENV = "GDX_INTERNAL_TOKEN"  # noqa: S105

#: What an unauthorised caller is told. Deliberately constant and uninformative:
#: it must not reveal whether the gate is misconfigured (which advertises a
#: window) or merely presented the wrong token. The specific reason goes to the
#: server log, where the operator who can act on it will see it.
REFUSAL_DETAIL = "internal token required"

#: Environments where the gate is deliberately off so a fresh clone and the test
#: suite work with no token set. Everything else — including an unrecognised
#: value — enforces. See the module docstring for why this is a denylist of
#: dev names rather than an allowlist of prod names.
#:
#: KNOWN LIMIT — the empty string is in here, so an *unset* GDX_ENV does not
#: enforce. That matches core.inbound_email_auth (the precedent this module
#: follows) and core/pii.py, but it is the OPPOSITE of gdx_dispatch/app.py:1114
#: (`is_prod = env in ("", "prod", "production")`), which reads unset as
#: production. Two conventions for one variable is a defect in its own right;
#: it is filed separately rather than resolved here, because reconciling them
#: touches every gate that reads GDX_ENV.
#:
#: What this costs in practice: every real deployment sets GDX_ENV explicitly
#: (prod and demo both run `production`, verified 2026-09-09; the tracked
#: compose defaults it to `dev`), so the unset case is the test suite and a
#: bare `docker run`. A deployment that leaves GDX_ENV at the compose `dev`
#: default keeps this gate OFF — that is the live limit of this fix, and it is
#: named here rather than left for someone to discover.
_NON_ENFORCING_ENVS = frozenset({"", "dev", "development", "test", "testing", "local", "ci"})


#: Domain separator for the derived token. Bump the suffix to rotate every
#: derived token at once without touching SECRET_KEY.
_DERIVATION_INFO = b"gdx-internal-token-v1"


def token() -> str:
    """The shared secret: the explicit one if set, else derived from SECRET_KEY.

    The derivation is what makes fail-closed deployable. Without it this change
    is a coordinated-deploy-or-outage: production runs
    ``docker-compose.yml + selfhost.yml + serverports.yml`` with no
    ``secrets-init`` to mint anything, and the public demo runs an **untracked**
    compose file that no pull request can edit. Requiring a new variable would
    have taken down the browser stream, plugin events and the restart hook on
    both stacks the moment the image rolled — and left demo unfixable from git.

    ``SECRET_KEY`` is the right seed because the containers that must agree
    already agree on it, and the one that must not know it already doesn't:

    * verified 2026-09-09 — app, plugin-host and all three celery containers on
      prod share one SECRET_KEY fingerprint; demo's two share a different one;
    * ``n8n`` receives neither ``SECRET_KEY`` nor the shared ``x-app-env``
      block, so the untrusted workflow container still cannot compute this.

    HMAC is one-way, so holding the derived token does not reveal SECRET_KEY or
    help against anything else signed with it. An explicit ``GDX_INTERNAL_TOKEN``
    still wins, so the token can be rotated on its own.

    Returns ``""`` when there is neither — and an empty token in an enforcing
    environment refuses, which is the fail-closed half.
    """
    explicit = os.getenv(TOKEN_ENV, "").strip()
    if explicit:
        return explicit
    seed = os.getenv("SECRET_KEY", "").strip()
    if not seed:
        return ""
    return hmac.new(seed.encode("utf-8"), _DERIVATION_INFO, hashlib.sha256).hexdigest()


def enforced() -> bool:
    """True when ``/internal/*`` callers must present the shared secret.

    Enforced when ``GDX_ENV`` is anything other than a known dev/test name, and
    also whenever a token is configured at all.
    """
    env = os.getenv("GDX_ENV", "").strip().lower()
    if env not in _NON_ENFORCING_ENVS:
        return True
    # Deliberately the EXPLICIT token, not token(): SECRET_KEY has a compose
    # default, so keying this on the derived value would silently turn every
    # dev box into an enforcing one — a surprise, not a decision.
    return bool(os.getenv(TOKEN_ENV, "").strip())


def rejection_reason(presented: str) -> str | None:
    """``None`` when the caller may proceed, else the reason — **for the log**.

    Callers must answer the network with the constant ``REFUSAL_DETAIL``, never
    with this string: it distinguishes "the gate is unconfigured" from "you sent
    the wrong token", which is exactly the distinction an unauthenticated caller
    should not be handed.

    Shared by the HTTP middleware and the WebSocket gate so the two can never
    drift apart — they did before: Starlette http middleware never runs for
    websocket scope, so the socket has to gate itself, and it carried its own
    copy of the rule.
    """
    if not enforced():
        return None
    secret = token()
    if not secret:
        # Fail closed. An unset token in a non-dev environment is a
        # misconfiguration, not permission to serve the operator's stored
        # credentials to anything on the network.
        return "internal auth is not configured (set GDX_INTERNAL_TOKEN)"
    # Compare as bytes: hmac.compare_digest raises TypeError on str operands
    # containing non-ASCII, and Starlette latin-1-decodes header values.
    if not hmac.compare_digest(presented.encode("utf-8"), secret.encode("utf-8")):
        return "internal token required"
    return None

def fingerprint() -> str:
    """A short, non-secret id for the token this container holds.

    The derivation makes the two sides' agreement *invisible*: if SECRET_KEY
    diverges, or an explicit GDX_INTERNAL_TOKEN is applied to some containers
    and not others, plugin events, the restart hook, the credential store and
    the browser stream all stop at once while `docker ps` shows every container
    healthy. Nothing in the stack checks the agreement — this at least makes it
    *observable*: every side logs its fingerprint at boot, so "do they match?"
    is a one-second comparison instead of a guess.

    It is a hash OF the token (itself already an HMAC of SECRET_KEY), truncated,
    so publishing it reveals neither the token nor the seed. Empty when no token
    is available, which is itself the answer.
    """
    tok = token()
    if not tok:
        return ""
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()[:12]


def log_identity(log, who: str) -> None:
    """Log this container's internal-auth identity at boot. Never raises.

    WARNING, not INFO, on purpose. plugin-host never configures root logging —
    uvicorn configures only its own loggers — so root sits at WARNING with no
    handler and INFO records are dropped entirely. This line's whole job is to
    be comparable between the app and plugin-host, and at INFO it would print on
    exactly one of the two. At WARNING it escapes via logging.lastResort even
    with no handler configured, which is how the refusal logs already surface.
    """
    try:
        fp = fingerprint()
        log.warning(
            "internal_auth %s: enforced=%s source=%s fingerprint=%s",
            who,
            enforced(),
            "explicit" if os.getenv(TOKEN_ENV, "").strip()
            else ("derived" if fp else "NONE"),
            fp or "<none>",
        )
    except Exception:  # pragma: no cover - diagnostics must never break boot
        log.debug("internal_auth identity logging failed", exc_info=True)
