"""Hard limits per request category.

These are the ceilings the policy layer enforces regardless of what the
decision engine reports. Adjust for the real business using this system.
"""

from src.requests.schema import RequestCategory

CATEGORY_LIMITS_USDC: dict[RequestCategory, float] = {
    RequestCategory.CONTRACTOR_MILESTONE: 2000.0,
    RequestCategory.SUPPLY_PURCHASE: 1000.0,
    RequestCategory.OPERATIONAL_EXPENSE: 500.0,
    RequestCategory.OTHER: 200.0,
}


def get_limit_for_category(category: RequestCategory) -> float:
    """Return the hard USDC ceiling for a given category."""
    return CATEGORY_LIMITS_USDC.get(category, CATEGORY_LIMITS_USDC[RequestCategory.OTHER])
