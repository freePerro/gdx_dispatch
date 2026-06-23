"""Version-compare logic for the self-host /api/admin/update-check endpoint.

Only the non-trivial bit (does 'latest > current' fire correctly, and never
for non-numeric tags) is checked — the HTTP fetch is stdlib/httpx, not ours.
"""
from gdx_dispatch.routers.admin_ops import _ver_tuple


def _update_available(current: str, latest: str) -> bool:
    cur_t, lat_t = _ver_tuple(current), _ver_tuple(latest)
    return bool(cur_t and lat_t and lat_t > cur_t)


def test_update_available():
    assert _update_available("1.2.0", "1.3.0") is True
    assert _update_available("1.9.0", "1.10.0") is True   # not string-compared
    assert _update_available("v1.2.0", "v1.2.0") is False
    assert _update_available("1.2.0", "1.1.9") is False    # downgrade
    # Non-numeric tags must never false-alarm an update.
    assert _update_available("dev", "1.3.0") is False
    assert _update_available("latest", "1.3.0") is False
    assert _update_available("1.2.0", "") is False


if __name__ == "__main__":
    test_update_available()
    print("ok")
