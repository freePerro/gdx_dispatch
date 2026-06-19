from gdx_dispatch.core.auth_capabilities import (
    caps_for_role,
    derive_ai_worker_caps,
)


def test_caps_for_role_exact_matches():
    assert caps_for_role("admin") == (("*", "*"),)
    assert caps_for_role("owner") == (("*", "*"),)
    assert caps_for_role("technician") == (
        ("read", "customer"),
        ("read", "job"),
        ("read", "schedule"),
        ("read", "parts"),
        ("write", "job.own"),
    )
    assert caps_for_role("customer") == (
        ("read", "customer.own"),
        ("read", "invoice.own"),
        ("read", "job.own"),
    )
    assert caps_for_role("ai_worker") == ()


def test_caps_for_role_case_insensitivity():
    assert caps_for_role("Admin") == (("*", "*"),)
    assert caps_for_role("TECHNICIAN") == (
        ("read", "customer"),
        ("read", "job"),
        ("read", "schedule"),
        ("read", "parts"),
        ("write", "job.own"),
    )


def test_caps_for_role_unknown():
    assert caps_for_role("nonexistent") == ()
    assert caps_for_role("") == ()


def test_derive_ai_worker_caps_strips_wildcards():
    admin_caps = [("*", "*"), ("read", "customer"), ("write", "job")]
    derived = derive_ai_worker_caps(admin_caps)
    # Should strip ("*", "*")
    assert ("*", "*") not in derived
    # Should keep read
    assert ("read", "customer") in derived
    # Should keep write (if it's not in red-tier and not wildcard)
    # Wait, the rule says: Narrows ("write", "*") and ("write", "<resource>") to S2 whitelist.
    # "write", "job" is not "write", "*" and not "write", "customer.contact".
    # So it should be dropped.
    assert ("write", "job") not in derived


def test_derive_ai_worker_caps_superuser_wildcard_expands_to_read_set():
    """An admin's ("*","*") wildcard expands to the read fan-out + write whitelist.

    Without this, admins (whose canonical caps ARE just (("*","*"),)) would
    derive an empty AI-worker cap set and the AI Assistant would have zero
    tools available — which is what shipped initially in 1.x and broke
    /api/ai/ask with "tools: Input should be a valid list" because the
    Anthropic SDK then served null in place of tools=[].
    """
    derived = derive_ai_worker_caps([("*", "*")])
    assert ("*", "*") not in derived
    assert ("read", "customer") in derived
    assert ("read", "job") in derived
    assert ("read", "invoice") in derived
    assert ("read", "schedule") in derived
    assert ("read", "technician") in derived
    assert ("read", "email") in derived
    assert ("read", "document") in derived
    assert ("write", "customer.contact") in derived
    assert ("write", "email") in derived
    assert ("write", "email.draft") in derived
    assert ("write", "document") in derived
    assert ("write", "document.folder") in derived


def test_derive_ai_worker_caps_strips_admin_action():
    admin_caps = [("admin", "tenant"), ("read", "customer")]
    derived = derive_ai_worker_caps(admin_caps)
    assert ("admin", "tenant") not in derived
    assert ("read", "customer") in derived


def test_derive_ai_worker_caps_narrows_writes():
    # ("write", "*") fans out to every whitelisted write resource.
    fanout = derive_ai_worker_caps([("write", "*")])
    assert ("write", "customer.contact") in fanout
    assert ("write", "email") in fanout
    assert ("write", "email.draft") in fanout
    assert ("write", "document") in fanout
    assert ("write", "document.folder") in fanout
    assert all(action == "write" for action, _ in fanout)

    # Whitelisted single-resource writes pass through unchanged.
    assert derive_ai_worker_caps([("write", "customer.contact")]) == (("write", "customer.contact"),)
    assert derive_ai_worker_caps([("write", "document")]) == (("write", "document"),)
    assert derive_ai_worker_caps([("write", "email.draft")]) == (("write", "email.draft"),)

    # Off-whitelist writes are dropped.
    assert derive_ai_worker_caps([("write", "other")]) == ()


def test_derive_ai_worker_caps_red_tier():
    # Test ("write", "invoice") -> []
    assert derive_ai_worker_caps([("write", "invoice")]) == ()
    # Test ("delete", "*") -> []
    assert derive_ai_worker_caps([("delete", "*")]) == ()
    # Test ("void", "*") -> []
    assert derive_ai_worker_caps([("void", "*")]) == ()


def test_derive_ai_worker_caps_keeps_reads():
    read_caps = [("read", "customer"), ("read", "job")]
    assert derive_ai_worker_caps(read_caps) == tuple(sorted(read_caps))


def test_derive_ai_worker_caps_dedup_and_sort():
    mixed_caps = [("read", "customer"), ("write", "customer.contact"), ("read", "customer")]
    # Duplicates collapse, output is sorted.
    expected = (("read", "customer"), ("write", "customer.contact"))
    assert derive_ai_worker_caps(mixed_caps) == expected
