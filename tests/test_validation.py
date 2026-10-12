"""Unit tests for the Excel validation rules. No database needed."""

from datetime import date, datetime

import pandas as pd
import pytest

from ingest.load_excel import validate_defect, validate_defects, validate_order, validate_orders

KNOWN_ORDERS = {"PO-1", "PO-2"}


def good_order(**changes) -> dict:
    """A valid order row; keyword arguments override single fields."""
    row = {
        "order_id": "PO-1",
        "customer_sector": "Medical",
        "product_type": "HDI",
        "layers": 8,
        "quantity": 100,
        "status": "In QA",
        "production_line": "Line 1",
        "created_date": date(2026, 7, 1),
        "due_date": date(2026, 7, 20),
    }
    row.update(changes)
    return row


def good_defect(**changes) -> dict:
    """A valid defect row; keyword arguments override single fields."""
    row = {
        "defect_id": "D-00001",
        "order_id": "PO-1",
        "detected_date": date(2026, 7, 5),
        "station": "AOI",
        "defect_type": "Short",
        "quantity": 3,
    }
    row.update(changes)
    return row


def assert_rejected(result, expected_text: str) -> None:
    """The row is rejected and one of the errors mentions expected_text."""
    clean, errors = result
    assert clean is None
    assert any(expected_text in error for error in errors), errors


# --- Orders -----------------------------------------------------------------


def test_valid_order_passes():
    clean, errors = validate_order(good_order())
    assert errors == []
    assert clean["order_id"] == "PO-1"


def test_order_values_are_normalised():
    # Excel often gives floats for whole numbers and Timestamps for dates.
    clean, errors = validate_order(
        good_order(
            order_id="  PO-1 ",
            layers=8.0,
            created_date=pd.Timestamp("2026-07-01"),
            due_date="2026-07-20",
        )
    )
    assert errors == []
    assert clean["order_id"] == "PO-1"
    assert clean["layers"] == 8 and isinstance(clean["layers"], int)
    assert clean["created_date"] == date(2026, 7, 1)
    assert clean["due_date"] == date(2026, 7, 20)


@pytest.mark.parametrize("missing", [None, "", "   ", float("nan")])
def test_missing_order_id_is_rejected(missing):
    assert_rejected(validate_order(good_order(order_id=missing)), "order_id is missing")


@pytest.mark.parametrize(
    "field, value",
    [
        ("customer_sector", "Automotive"),
        ("product_type", "Ceramic"),
        ("status", "Done"),
    ],
)
def test_unknown_category_is_rejected(field, value):
    assert_rejected(validate_order(good_order(**{field: value})), f"unknown {field} '{value}'")


@pytest.mark.parametrize("layers", [0, 1, 17, 18])
def test_layers_out_of_range_is_rejected(layers):
    assert_rejected(validate_order(good_order(layers=layers)), "layers must be between 2 and 16")


@pytest.mark.parametrize("layers", [2, 16])
def test_layers_at_the_limits_are_accepted(layers):
    _, errors = validate_order(good_order(layers=layers))
    assert errors == []


@pytest.mark.parametrize("quantity", [-50, 0])
def test_non_positive_order_quantity_is_rejected(quantity):
    assert_rejected(validate_order(good_order(quantity=quantity)), "quantity must be at least 1")


@pytest.mark.parametrize("quantity", [12.5, "abc"])
def test_non_whole_quantity_is_rejected(quantity):
    assert_rejected(validate_order(good_order(quantity=quantity)), "is not a whole number")


def test_missing_production_line_is_rejected():
    assert_rejected(validate_order(good_order(production_line=None)), "production_line is missing")


def test_missing_date_is_rejected():
    assert_rejected(validate_order(good_order(created_date=pd.NaT)), "created_date is missing")


def test_invalid_date_text_is_rejected():
    assert_rejected(validate_order(good_order(due_date="31/07/2026")), "is not a valid date")


def test_due_date_before_created_date_is_rejected():
    result = validate_order(good_order(created_date=date(2026, 7, 10), due_date=date(2026, 7, 5)))
    assert_rejected(result, "due_date 2026-07-05 is before created_date 2026-07-10")


def test_due_date_equal_to_created_date_is_accepted():
    _, errors = validate_order(good_order(created_date=date(2026, 7, 10), due_date=datetime(2026, 7, 10)))
    assert errors == []


def test_all_problems_of_a_row_are_reported():
    _, errors = validate_order(good_order(quantity=-1, status="Done"))
    assert len(errors) == 2


def test_duplicate_order_id_keeps_first_and_rejects_later():
    df = pd.DataFrame([good_order(), good_order(order_id="PO-2"), good_order(quantity=999)])
    valid, rejected = validate_orders(df)

    assert [row["order_id"] for row in valid] == ["PO-1", "PO-2"]
    assert valid[0]["quantity"] == 100
    assert len(rejected) == 1
    assert rejected[0]["excel_row"] == 4
    assert "duplicate order_id PO-1 (first seen on Excel row 2)" in rejected[0]["reason"]


def test_valid_row_after_invalid_copy_is_not_a_duplicate():
    df = pd.DataFrame([good_order(quantity=-1), good_order()])
    valid, rejected = validate_orders(df)

    assert len(valid) == 1
    assert len(rejected) == 1
    assert "duplicate" not in rejected[0]["reason"]


def test_rejected_row_keeps_original_values_and_reason():
    df = pd.DataFrame([good_order(status="Done")])
    _, rejected = validate_orders(df)

    assert rejected[0]["status"] == "Done"
    assert rejected[0]["excel_row"] == 2
    assert "unknown status 'Done'" in rejected[0]["reason"]


# --- Defects ----------------------------------------------------------------


def test_valid_defect_passes():
    clean, errors = validate_defect(good_defect(), KNOWN_ORDERS)
    assert errors == []
    assert clean["defect_id"] == "D-00001"


def test_defect_for_non_existent_order_is_rejected():
    result = validate_defect(good_defect(order_id="PO-99999"), KNOWN_ORDERS)
    assert_rejected(result, "order_id PO-99999 does not exist in orders")


@pytest.mark.parametrize("field", ["defect_id", "order_id", "detected_date"])
def test_missing_defect_field_is_rejected(field):
    assert_rejected(validate_defect(good_defect(**{field: None}), KNOWN_ORDERS), f"{field} is missing")


def test_unknown_station_is_rejected():
    result = validate_defect(good_defect(station="Packing"), KNOWN_ORDERS)
    assert_rejected(result, "unknown station 'Packing'")


def test_unknown_defect_type_is_rejected():
    result = validate_defect(good_defect(defect_type="Scratch"), KNOWN_ORDERS)
    assert_rejected(result, "unknown defect_type 'Scratch'")


@pytest.mark.parametrize("quantity", [-3, 0])
def test_non_positive_defect_quantity_is_rejected(quantity):
    result = validate_defect(good_defect(quantity=quantity), KNOWN_ORDERS)
    assert_rejected(result, "quantity must be at least 1")


def test_duplicate_defect_id_is_rejected():
    df = pd.DataFrame([good_defect(), good_defect(order_id="PO-2")])
    valid, rejected = validate_defects(df, KNOWN_ORDERS)

    assert len(valid) == 1
    assert "duplicate defect_id D-00001" in rejected[0]["reason"]
