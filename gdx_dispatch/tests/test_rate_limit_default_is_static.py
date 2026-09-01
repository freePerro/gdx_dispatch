"""The slowapi default rate limit must be a static string, never a callable.

Why this guard exists (2026-09-01). `app.py` used to set
`default_limits=[_tier_limit]`, where `_tier_limit` tried to read an
`x-tenant-tier` request header and grant 600/minute for "professional" instead
of the 120/minute default. That looked like a client-supplied 5x rate-limit
raise. It was not — it was dead code, and the truth is worse in a quieter way.

slowapi resolves a callable `default_limits` provider like this
(`slowapi/wrappers.py`):

    if callable(self.__limit_provider):
        if "key" in inspect.signature(self.__limit_provider).parameters.keys():
            limit_raw = self.__limit_provider(self.key_function(self.request))
        else:
            limit_raw = self.__limit_provider()

The provider is called with **no arguments** unless its signature has a
parameter named exactly `key` — and even then it receives the *key* (the client
IP), never the request. So no `default_limits` callable can read a header, and
`_tier_limit` always fell through to its 120/minute default. Measured against a
running container: 120 requests then 429, identically with and without the
header. Its E2E bypass branch was dead for the same reason.

The failure mode this guards is subtle: someone "fixes" the signature so the
header finally works, and silently ships a client-controlled rate limit. Pinning
the default to a plain string makes that impossible to do by accident.
"""
from __future__ import annotations

import inspect


def _providers():
    from gdx_dispatch.app import limiter

    # slowapi stores parsed default limits; reach the raw providers behind them.
    groups = getattr(limiter, "_default_limits", None)
    assert groups, "limiter has no default limits — rate limiting would be off"
    return groups


def test_default_limit_is_declared_as_a_plain_string():
    from gdx_dispatch.app import DEFAULT_RATE_LIMIT

    assert isinstance(DEFAULT_RATE_LIMIT, str)
    assert DEFAULT_RATE_LIMIT == "120/minute"


def test_no_default_limit_provider_is_a_callable():
    """The defect class: a request-dependent default limit slowapi cannot feed."""
    offenders = []
    for group in _providers():
        provider = getattr(group, "_LimitGroup__limit_provider", None)
        if provider is None:  # attribute name differs across versions
            provider = next(
                (v for k, v in vars(group).items() if "limit_provider" in k), None
            )
        if callable(provider):
            offenders.append(getattr(provider, "__name__", repr(provider)))
    assert offenders == [], (
        "slowapi default_limits must be static strings — these are callables and "
        f"cannot receive the request: {offenders}"
    )


def test_a_callable_provider_could_not_read_a_request_anyway():
    """Pins slowapi's calling convention, so this guard's premise stays true.

    If a future slowapi passes the request, this test fails and the reasoning
    above (and the comment in app.py) must be revisited rather than trusted.
    """
    from slowapi.wrappers import LimitGroup

    src = inspect.getsource(LimitGroup)
    assert '"key" in inspect.signature' in src, (
        "slowapi changed how it invokes limit providers — re-check whether a "
        "callable default_limits can now see the request"
    )
    assert "self.__limit_provider()" in src, (
        "slowapi no longer calls providers with no arguments — re-check app.py"
    )
