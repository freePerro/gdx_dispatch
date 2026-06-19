"""Tests for the instant estimate description parser — pure function, no DB needed."""
import pytest

from gdx_dispatch.routers.instant_estimate import _parse_description


def test_standard_description():
    result = _parse_description("16x7 insulated steel door replacement with springs")
    assert result["width"] == 16
    assert result["height"] == 7
    assert result["material"] == "steel"
    assert result["job_type"] == "replacement"
    assert "spring" in result["part_keywords"]


@pytest.mark.parametrize("desc, expected_w, expected_h", [
    ("9x8 door", 9, 8),
    ("16 x 7 frame", 16, 7),
    ("16X7 panel", 16, 7),
    ("10 x 10 custom", 10, 10),
    ("replace 8x7 steel", 8, 7),
])
def test_dimension_formats(desc, expected_w, expected_h):
    result = _parse_description(desc)
    assert result["width"] == expected_w
    assert result["height"] == expected_h


def test_no_dimensions():
    result = _parse_description("fix the broken spring")
    assert result["width"] is None
    assert result["height"] is None
    assert result["job_type"] == "repair"
    assert "spring" in result["part_keywords"]


@pytest.mark.parametrize("desc, expected_material", [
    ("wood door", "wood"),
    ("aluminum frame", "aluminum"),
    ("fiberglass panel", "fiberglass"),
    ("steel garage door", "steel"),
    ("vinyl siding", "vinyl"),
])
def test_material_detection(desc, expected_material):
    result = _parse_description(desc)
    assert result["material"] == expected_material


def test_no_material():
    result = _parse_description("fix the door")
    assert result["material"] is None


@pytest.mark.parametrize("desc, expected_job", [
    ("replacement door", "replacement"),
    ("replace the old door", "replacement"),
    ("repair the spring", "repair"),
    ("fix broken cable", "repair"),
    ("broken spring needs fixing", "repair"),
    ("install new opener", "installation"),
    ("new door needed", "installation"),
    ("service call for maintenance", "service"),
    ("maintenance visit", "maintenance"),
])
def test_job_type_detection(desc, expected_job):
    result = _parse_description(desc)
    assert result["job_type"] == expected_job


def test_multiple_part_keywords():
    result = _parse_description("replace spring and cable, install new opener and remote")
    assert "spring" in result["part_keywords"]
    assert "cable" in result["part_keywords"]
    assert "opener" in result["part_keywords"]
    assert "remote" in result["part_keywords"]


def test_single_part_keywords():
    for kw in ["spring", "cable", "track", "roller", "hinge", "seal",
                "opener", "remote", "keypad", "sensor", "panel"]:
        result = _parse_description(f"replace the {kw}")
        assert kw in result["part_keywords"], f"Expected '{kw}' in part_keywords"


def test_empty_string():
    result = _parse_description("")
    assert result["width"] is None
    assert result["height"] is None
    assert result["material"] is None
    assert result["job_type"] == "service"  # default
    assert result["part_keywords"] == []


def test_sql_injection_attempt():
    result = _parse_description("'; DROP TABLE users; -- 16x7 steel")
    assert result["width"] == 16
    assert result["height"] == 7
    assert result["material"] == "steel"


def test_xss_attempt():
    result = _parse_description('<script>alert("xss")</script> 9x8 wood replacement')
    assert result["width"] == 9
    assert result["height"] == 8
    assert result["material"] == "wood"
    assert result["job_type"] == "replacement"


def test_very_long_description():
    long_desc = "a " * 500 + "16x7 steel replacement spring" + " b" * 500
    result = _parse_description(long_desc)
    assert result["width"] == 16
    assert result["height"] == 7
    assert result["material"] == "steel"
    assert result["job_type"] == "replacement"
    assert "spring" in result["part_keywords"]


def test_unicode_description():
    result = _parse_description("16x7 steel door — replacement with torsion springs")
    assert result["width"] == 16
    assert result["height"] == 7
    assert result["job_type"] == "replacement"
    assert "spring" in result["part_keywords"]


def test_raw_field_preserved():
    desc = "16x7 insulated steel door"
    result = _parse_description(desc)
    assert result["raw"] == desc


def test_case_insensitive_material():
    result = _parse_description("STEEL door 16x7")
    assert result["material"] == "steel"


def test_multiple_dimensions_takes_first():
    result = _parse_description("16x7 door and 9x8 window")
    assert result["width"] == 16
    assert result["height"] == 7


# --- Additional edge cases ---

def test_multiple_materials_takes_first():
    """When multiple materials mentioned, first match wins."""
    result = _parse_description("steel and wood door")
    assert result["material"] == "steel"


def test_job_type_priority_replace_over_broken():
    """'replace' keyword should yield 'replacement' even if 'broken' is also present."""
    result = _parse_description("replace broken spring")
    assert result["job_type"] == "replacement"


def test_no_job_type_defaults_to_service():
    """Description with no job type keyword defaults to 'service'."""
    result = _parse_description("check the door sensors")
    assert result["job_type"] == "service"


def test_description_with_numbers_only():
    """Just dimensions, nothing else."""
    result = _parse_description("16x7")
    assert result["width"] == 16
    assert result["height"] == 7


def test_description_with_customer_name():
    """Customer name in description shouldn't break parsing."""
    result = _parse_description("Jon Krause needs a 16x7 steel door replacement")
    assert result["width"] == 16
    assert result["height"] == 7
    assert result["material"] == "steel"
    assert result["job_type"] == "replacement"


def test_description_with_price():
    """Price in description shouldn't be confused with dimensions."""
    result = _parse_description("$1200 16x7 steel door replacement")
    assert result["width"] == 16
    assert result["height"] == 7
