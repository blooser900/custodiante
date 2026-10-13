"""Exact USDC amounts (review R2-04).

Floats carry the number a human typed; what goes on the wire is rendered from a
Decimal. These tests pin the cases where the two differ.
"""

from decimal import Decimal

import pytest

from src.money import (
    MAX_USDC,
    InvalidAmountError,
    format_usdc,
    parse_usdc,
    quantize_usdc,
    sum_usdc,
    to_decimal,
    usdc_to_base_units,
)


# -- the float behaviour this module exists to avoid -------------------------


def test_the_float_formatting_that_motivated_this_module():
    """If these ever stop being true the module is dead weight; today they are
    exactly what `str(float)` would have put in the request to Circle."""
    assert str(0.000001) == "1e-06"
    assert str(0.1 + 0.2) == "0.30000000000000004"
    assert 333.33 * 0.35 != 116.6655  # it is 116.66549999999999


@pytest.mark.parametrize(("value", "text"), [
    (100, "100"),
    (100.0, "100"),
    (5.0, "5"),
    (100.5, "100.5"),
    (0.000001, "0.000001"),
    (1e-06, "0.000001"),
    (1234.56789, "1234.56789"),
    (0.1, "0.1"),
    ("12.50", "12.5"),
    (Decimal("7.000"), "7"),
    (Decimal("1E+2"), "100"),
])
def test_format_usdc_is_plain_decimal_text(value, text):
    assert format_usdc(value) == text


# -- what is refused ---------------------------------------------------------


@pytest.mark.parametrize("bad", [
    0, -1, -0.000001, 0.0,
    float("nan"), float("inf"), float("-inf"),
    0.1 + 0.2,           # 17 decimal places: float noise, not an amount
    1.0000001,           # 7 decimal places
    1e-7,
    "abc", "", None, [], True, False,
    float(MAX_USDC) * 10,
])
def test_parse_usdc_refuses_what_cannot_be_paid(bad):
    with pytest.raises(InvalidAmountError):
        parse_usdc(bad)


def test_the_error_is_a_value_error_so_existing_handlers_still_catch_it():
    assert issubclass(InvalidAmountError, ValueError)


def test_the_message_says_what_is_wrong():
    with pytest.raises(InvalidAmountError, match="more than 6 decimal places"):
        parse_usdc(1.0000001)
    with pytest.raises(InvalidAmountError, match="must be positive"):
        parse_usdc(0)
    with pytest.raises(InvalidAmountError, match="finite"):
        parse_usdc(float("inf"))


def test_amounts_at_the_edges_of_what_is_valid_are_accepted():
    assert parse_usdc(0.000001) == Decimal("0.000001")
    assert parse_usdc(MAX_USDC) == MAX_USDC


# -- reading what a human typed ----------------------------------------------


def test_a_float_is_read_as_the_decimal_that_was_written_not_its_binary_expansion():
    assert to_decimal(0.1) == Decimal("0.1")          # not 0.1000000000000000055...
    assert to_decimal(116.6655) == Decimal("116.6655")
    assert to_decimal(5) == Decimal(5)


# -- computed amounts --------------------------------------------------------


def test_a_computed_amount_is_rounded_down_never_up():
    assert quantize_usdc(Decimal("33.4999996649")) == Decimal("33.499999")
    assert quantize_usdc(Decimal("0.0000009")) == Decimal("0")


def test_decimal_arithmetic_gives_the_figure_the_float_one_gets_wrong():
    assert to_decimal(333.33) * to_decimal(0.35) == Decimal("116.6655")


# -- the contract's integer --------------------------------------------------


@pytest.mark.parametrize(("usdc", "base"), [
    (1, 1_000_000),
    (1.5, 1_500_000),
    (0.000001, 1),
    (116.6655, 116_665_500),
    (1234567.891234, 1_234_567_891_234),
])
def test_usdc_to_base_units_is_exact(usdc, base):
    assert usdc_to_base_units(usdc) == base


@pytest.mark.parametrize("bad", [0, -1, 1.0000001, float("nan"), float("inf")])
def test_usdc_to_base_units_refuses_what_cannot_be_exact(bad):
    with pytest.raises(ValueError):
        usdc_to_base_units(bad)


# -- totals ------------------------------------------------------------------


def test_sums_add_up_exactly():
    assert sum(0.1 for _ in range(3)) != 0.3  # the float total is 0.30000000000000004
    assert sum_usdc([0.1, 0.1, 0.1]) == Decimal("0.3")
    assert sum_usdc([]) == Decimal(0)


def test_a_total_still_adds_older_amounts_that_have_more_precision_than_usdc():
    """Rows written before amounts were validated may have 7+ places. A dashboard
    total must not raise because of one of them."""
    assert sum_usdc([1.0000001, 2]) == Decimal("3.0000001")
