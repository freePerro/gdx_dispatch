"""Derive-fallback for catalog pricing_category. Guards the money path: an
item must never land with no bucket (→ $0 estimate line) and common garage-door
category words must map to the right tier bucket."""
from gdx_dispatch.routers.catalog import _derive_pricing_category

VALID = {"doors", "openers", "parts", "labor", "other"}


def d(explicit=None, category=None, product_class=None):
    return _derive_pricing_category(explicit, category, product_class, VALID)


def test_explicit_wins():
    assert d(explicit="openers", category="remote") == "openers"


def test_exact_category():
    assert d(category="doors") == "doors"


def test_singular_maps_to_plural():
    assert d(category="opener") == "openers"
    assert d(category="door") == "doors"


def test_domain_synonyms_to_parts():
    for word in ("remote", "remotes", "keypad", "spring", "cable", "track"):
        assert d(category=word) == "parts", word


def test_operator_is_opener():
    assert d(category="operator") == "openers"


def test_labor_never_derived_to_tier_bucket():
    # labor lines use the matrix, not the engine — must not become a tier bucket
    assert d(category="labor", product_class="labor") is None


def test_unknown_falls_back_to_product_class():
    assert d(category="whatsit", product_class="opener") == "openers"


def test_unknown_everything_is_other_not_none():
    # the key invariant: never None for a non-labor item → never $0
    assert d(category="whatsit", product_class="mystery") == "other"
    assert d() == "other"


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"\n{len(fns)} checks passed")
    sys.exit(0)
