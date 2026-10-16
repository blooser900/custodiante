"""The ceiling a category limit may not exceed (R2-09).

A category limit only ever had a FLOOR (`max_usdc > 0`). Nothing stopped a
manager -- or a typo in the dashboard, or a `PUT /api/config` -- from setting a
category ceiling to a number larger than the treasury actually holds, which
turns "auto-approve under the limit" into "auto-approve everything". The limit
is the control; a limit above the money available is not a control.

Where the ceiling comes from: the wallet's own USDC balance, read through the
same `ArcPaymentClient.get_balance()` the payment path uses -- the function that
was written and then never called (ALT-11). The balance is the honest answer to
"what could this system actually pay?".

`ceiling_for` takes the balance, with BOTH ends of the range pinned:

    min(max(balance, MIN_FUNDED_USDC), MAX_LIMIT_USDC)

  * in the middle it follows the balance: with 5,000 USDC in the wallet, a
    category ceiling of 9,000 is authority the treasury cannot honour;
  * at the top it stops at `MAX_LIMIT_USDC` (100,000), so a very well-funded
    wallet does not turn a policy decision into a mirror of its balance. This is
    the author's rule: "the wallet's total balance, or something like 10**5 when
    get_balance() returns values larger than 10**5";
  * at the bottom it stops at `MIN_FUNDED_USDC` (1,000), so a wallet that has
    not been funded yet can still have its rules configured before the first
    deposit. Without that floor, a zero balance would permit no limit at all.

FAILS OPEN, deliberately, and this is the part to know before trusting it:
reading the balance needs credentials and the network, so when Circle cannot be
reached the ceilings are NOT checked and ANY limit is accepted. A rules screen
that refuses to save because Circle is down would be uneditable exactly when
someone needs it, and this is a sanity guard, not a security boundary. The
operator is warned (a 200 response with a logged warning on the API, a message
on stderr from the CLI), but nothing is refused. The consequence, stated plainly:
this control is only active when the wallet is reachable.
"""

import logging
from dataclasses import dataclass

from src.config import RulesConfig

logger = logging.getLogger(__name__)

# The largest per-category limit allowed, whatever the wallet holds: past this
# the limit stops tracking the balance and becomes a policy maximum.
MAX_LIMIT_USDC = 100_000.0

# Below this, the balance is "not yet funded" and the ceiling stops following it.
# Without a floor, a brand-new wallet (balance 0) could not have ANY category
# limit, so a fresh deployment could not configure its rules before the first
# deposit. The floor is the smallest amount the system is expected to operate
# with, not a free pass: a 1,000 USDC balance still refuses a 2,000 limit.
MIN_FUNDED_USDC = 1_000.0


class LimitAboveCeilingError(ValueError):
    """A category ceiling is larger than the money available to back it."""


@dataclass(frozen=True)
class CeilingCheck:
    """What the ceiling turned out to be, and whether it could be checked."""

    # None when the balance could not be read: the check did not run.
    ceiling_usdc: float | None
    # The raw balance, when there was one -- for the message.
    balance_usdc: float | None
    # Categories whose ceiling is above `ceiling_usdc`, sorted.
    over: tuple[str, ...]
    detail: str

    @property
    def checked(self) -> bool:
        return self.ceiling_usdc is not None

    @property
    def ok(self) -> bool:
        return not self.over


def ceiling_for(balance_usdc: float) -> float:
    """The largest category limit this balance can back.

    The middle of the range follows the balance: with 5,000 USDC in the wallet,
    a category ceiling of 9,000 is authority the treasury cannot honour. The two
    ends of the range are pinned:

      * at or above `MAX_LIMIT_USDC` the ceiling stops moving, so a very
        well-funded wallet does not silently authorise enormous approvals --
        the limit stays a policy decision (100,000 USDC per category);
      * below `MIN_FUNDED_USDC` the ceiling stops falling, so a wallet that has
        not been funded yet can still have sane rules configured before its
        first deposit.

    The user's rule: "the wallet's total balance, or something like 10**5 when
    get_balance() returns values larger than 10**5".
    """
    return min(max(balance_usdc, MIN_FUNDED_USDC), MAX_LIMIT_USDC)


def wallet_balance() -> float:
    """The treasury's USDC balance, read through the payment client (ALT-11).

    Imported here rather than at module level: `arc_client` pulls in the Circle
    SDK, and this module is also imported on paths that never touch a wallet.
    """
    from src.payments.arc_client import ArcPaymentClient

    return ArcPaymentClient().get_balance()


def check_limits_against_balance(config: RulesConfig, balance_provider=None) -> CeilingCheck:
    """Compare every category ceiling with the money available.

    `balance_provider` is any zero-argument callable returning a float; the
    default reads the wallet through `ArcPaymentClient.get_balance()`. It raising
    means "cannot check", which is reported rather than treated as a failure:
    see the module docstring.
    """
    provider = balance_provider or wallet_balance
    try:
        balance = float(provider())
    except Exception as e:  # noqa: BLE001 -- any failure means "cannot check"
        detail = (
            f"Could not read the wallet balance, so the category ceilings were not "
            f"checked against the funds available: {type(e).__name__}: {e}"
        )
        logger.warning(detail)
        return CeilingCheck(None, None, (), detail)

    ceiling = ceiling_for(balance)
    over = tuple(
        sorted(
            name
            for name, limit in config.category_limits.items()
            if limit.max_usdc > ceiling
        )
    )
    if over:
        # Each offender with its own value: "lower contractor_milestone" is only
        # actionable once the operator knows it is the 5,000 one.
        listed = ", ".join(
            f"{name} ({config.category_limits[name].max_usdc:,.2f})" for name in over
        )
        detail = (
            f"{listed} exceed(s) the {ceiling:,.2f} USDC ceiling "
            f"(wallet balance: {balance:,.2f} USDC). A category limit above the funds "
            "available is not a limit: lower it, or fund the wallet."
        )
    else:
        detail = (
            f"Every category ceiling is within the {ceiling:,.2f} USDC ceiling "
            f"(wallet balance: {balance:,.2f} USDC)."
        )
    return CeilingCheck(ceiling, balance, over, detail)


def require_limits_within_balance(config: RulesConfig, balance_provider=None) -> CeilingCheck:
    """Like `check_limits_against_balance`, but raises when a limit is too high.

    For the two places that persist rules -- `PUT /api/config` and
    `manage_rules.py` -- where a limit above the balance should be refused
    rather than merely reported.
    """
    result = check_limits_against_balance(config, balance_provider)
    if not result.ok:
        raise LimitAboveCeilingError(result.detail)
    return result
