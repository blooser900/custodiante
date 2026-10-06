"""Hard limits per request category.

These are the ceilings the policy layer enforces regardless of what the
decision engine reports. Values come from config/rules.yaml (see
src/config.py) -- edit that file, via the CLI (scripts/manage_rules.py),
or via the dashboard's settings page, to adjust limits without touching code.
"""

from src.config import load_config
from src.requests.schema import RequestCategory


def get_limit_for_category(category: RequestCategory) -> float:
    """Return the hard USDC ceiling for a given category."""
    limits = load_config().category_limits
    return limits.get(category, limits.get("other", 200.0))
