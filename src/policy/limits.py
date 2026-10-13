"""Hard limits per request category.

These are the ceilings the policy layer enforces regardless of what the
decision engine reports. Values come from config/rules.yaml (see
src/config.py) -- edit that file, via the CLI (scripts/manage_rules.py),
or via the dashboard's settings page, to adjust limits without touching code.

A category also says whether a payment may be made *partially*
(`allow_reduced`). Half a supply purchase is still supply; half a contract
milestone is a breach of contract, so the policy escalates instead.
"""

from src.config import CategoryLimit, load_config
from src.requests.schema import RequestCategory


def _limit_for(category: RequestCategory) -> CategoryLimit:
    limits = load_config().category_limits
    entry = limits.get(category) or limits.get("other")
    if entry is None:
        # Unreachable through RulesConfig, which requires every category to be
        # present, but this module is also called with hand-built configs.
        return CategoryLimit(max_usdc=200.0, allow_reduced=True)
    return entry


def get_limit_for_category(category: RequestCategory) -> float:
    """Return the hard USDC ceiling for a given category."""
    return _limit_for(category).max_usdc


def reduced_payment_allowed(category: RequestCategory) -> bool:
    """Whether a part payment is a meaningful outcome for this category.

    False means the policy must escalate instead of approving a fraction: for a
    contractor milestone, paying half the agreed price is not half a delivery,
    it is paying for work that has not been accepted.
    """
    return _limit_for(category).allow_reduced
