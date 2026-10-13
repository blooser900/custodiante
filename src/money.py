"""Exact USDC amounts.

Amounts travel through the system as plain floats (request bodies, the audit log,
the dashboard all use JSON numbers), and that is fine for what a float is good at:
carrying a decimal the human typed. `0.1` round-trips as `0.1`, and `repr` gives
back the shortest text that names the same double, so `Decimal(repr(x))` recovers
the number that was written.

What a float is NOT good for is arithmetic that ends up on the wire. `0.1 + 0.2`
is `0.30000000000000004`, `333.33 * 0.35` is `116.66549999999999`, and
`str(0.000001)` is `'1e-06'`. USDC has 6 decimal places, so any of those is either
a rejected transfer or, worse, an amount nobody authorized. The rule here is:

  * an amount that enters the system is checked once, at the edge (`parse_usdc`);
  * an amount that is COMPUTED (the reduced approval) is quantized explicitly, and
    always rounded DOWN, so the system never authorizes more than the rule gave;
  * an amount that leaves the system (Circle, the escrow contract) is rendered
    from a `Decimal` (`format_usdc`, `usdc_to_base_units`), never from `str(float)`;
  * a total that is shown is summed as `Decimal` (`sum_usdc`).

Order comparisons (`amount > limit`) stay on floats on purpose: two decimals with
at most 15 significant digits keep their order as doubles, so they are exact for
every amount `parse_usdc` accepts.
"""

from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Iterable

USDC_DECIMALS = 6
_QUANTUM = Decimal(1).scaleb(-USDC_DECIMALS)  # Decimal("0.000001")

# A sanity ceiling, not a business rule (that is the category limit). It stops an
# absurd value such as 1e300 from reaching Decimal formatting or the chain, and it
# is ten times the whole supply of USDC, so no real payment is ever near it.
MAX_USDC = Decimal(10) ** 12


class InvalidAmountError(ValueError):
    """An amount that cannot be paid as written."""


def to_decimal(value) -> Decimal:
    """The decimal a human wrote, from a float, int, str or Decimal.

    Does not judge the value (see `parse_usdc`); it only refuses what is not a
    finite number at all, so NaN and infinity can never reach arithmetic.
    """
    if isinstance(value, bool):
        raise InvalidAmountError(f"{value!r} is not an amount.")
    try:
        if isinstance(value, Decimal):
            result = value
        elif isinstance(value, float):
            result = Decimal(repr(value))
        elif isinstance(value, (int, str)):
            result = Decimal(str(value).strip())
        else:
            raise InvalidAmountError(f"{value!r} is not an amount.")
    except InvalidOperation as e:
        raise InvalidAmountError(f"{value!r} is not a number.") from e
    if not result.is_finite():
        raise InvalidAmountError(f"{value!r} is not a finite number.")
    return result


def parse_usdc(value) -> Decimal:
    """Validate an amount to be PAID: positive, at most 6 decimal places, sane size."""
    amount = to_decimal(value)
    if amount <= 0:
        raise InvalidAmountError(f"{value!r} USDC must be positive.")
    if amount > MAX_USDC:
        raise InvalidAmountError(f"{value!r} USDC is above the {MAX_USDC} USDC sanity ceiling.")
    if amount != amount.quantize(_QUANTUM):
        raise InvalidAmountError(
            f"{value!r} USDC has more than {USDC_DECIMALS} decimal places."
        )
    return amount


def quantize_usdc(value, rounding=ROUND_DOWN) -> Decimal:
    """Round a COMPUTED amount to USDC's 6 places. Down by default: a rule that
    derives an amount must never authorize more than it computed."""
    return to_decimal(value).quantize(_QUANTUM, rounding=rounding)


def format_usdc(value) -> str:
    """The amount as the plain decimal string an API expects: no exponent, no
    float noise, no trailing zeros ('100', '0.5', '0.000001'). Validates first."""
    text = format(parse_usdc(value), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def usdc_to_base_units(value) -> int:
    """The integer the escrow contract uses (6 decimals). Exact, never rounds."""
    return int(parse_usdc(value).scaleb(USDC_DECIMALS))


def sum_usdc(values: Iterable) -> Decimal:
    """Total for display. Accepts any recorded amount, including older rows with
    more precision than USDC has: a total must still add up, not raise."""
    return sum((to_decimal(v) for v in values), Decimal(0))
