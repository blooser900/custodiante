"""The payee registry: which wallets a payee is allowed to be paid at.

Why this module exists. `history.py` keys the destination history by the wallet
*in the request*. That answers "has this wallet been paid cleanly before?", not
"is this the wallet of the vendor being invoiced?". Those come apart exactly
where it costs money: measured on real engine answers, a request that redirected
an established contractor's payment to a different account scored trust 0.806
(above the 0.75 auto-approve bar) and P(fraud) 0.362 (far below the 0.7 veto), so
the system paid it without a human. The new wallet had six clean payments of its
own, so it looked established. Nothing in the request contradicted it, because
nothing tied the vendor to any wallet.

The check that catches this is not a model judgement but an equality: the wallet
in the request must be one this payee is known to use. It does not depend on
confidence, on a probability, or on how big the model is.

Where the knowledge comes from
------------------------------
Two sources, merged, because either alone is insufficient:

  registered   `config/rules.yaml` (`vendor_registry`). A manager saying "this
               payee uses these wallets". Authoritative, and the only source
               that can authorise a *new* wallet deliberately.
  observed     the audit log. A payee that has already been paid successfully,
               and not refunded, at a wallet is observed to use it. Without
               this the registry would be empty on day one and every request
               would escalate until someone typed the whole vendor list in,
               which is a migration nobody asked for.

Observed wallets keep the protection honest as a payment history builds up, and
registered wallets are how a deliberate change (a vendor really did move banks)
is allowed through: a manager adds the new wallet instead of the policy guessing.

Fail-open on absence, fail-closed on contradiction
--------------------------------------------------
No entry at all for a payee means UNKNOWN, which escalates -- an unregistered
payee is not evidence of fraud, but it is a reason for a person to look. A move
between wallets the payee is not known to use is a MISMATCH, which also
escalates. Neither ever denies: the human reviewing the escalation can approve,
which is what keeps a false positive cheap.
"""

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from src.config import VendorRegistration

if TYPE_CHECKING:  # AuditEntry imports the engine, which imports history
    from src.audit.logger import AuditEntry

# Outcome states. UNKNOWN and MISMATCH both escalate; only KNOWN lets the
# automatic path continue.
UNKNOWN_PAYEE = "unknown_payee"
WALLET_MISMATCH = "wallet_mismatch"
KNOWN_WALLET = "known_wallet"

# Statuses we accept as proof a payment to a wallet actually went through.
# "refunded" (an escrow that was rejected) counts as NOT proof -- the money came
# back, so the destination was not accepted. "failed" likewise.
_PROOF_OF_USE = ("executed", "escrowed")


class PayeeStatus(str, Enum):
    UNKNOWN = UNKNOWN_PAYEE
    MISMATCH = WALLET_MISMATCH
    KNOWN = KNOWN_WALLET


@dataclass(frozen=True)
class PayeeAssessment:
    """What the registry knows about paying `payee` at `wallet`."""

    status: PayeeStatus
    payee: str
    wallet: str | None
    known_wallets: tuple[str, ...]
    detail: str


def normalize_key(value: str) -> str:
    """The same normalization `history.py` uses for requesters."""
    return value.strip().lower()


def normalize_wallet(value: str | None) -> str | None:
    wallet = (value or "").strip().lower()
    return wallet or None


class PayeeRegistry:
    """Known payees and the wallets they use, from config and from the log."""

    def __init__(self, registered: dict[str, VendorRegistration] | None = None) -> None:
        # key -> wallet -> where that knowledge came from
        self._sources: dict[str, dict[str, str]] = {}
        for key, registration in (registered or {}).items():
            wallets = self._sources.setdefault(normalize_key(key), {})
            for wallet in registration.wallets:
                wallets[normalize_wallet(wallet) or ""] = "registered"

    @classmethod
    def from_config(cls, config=None) -> "PayeeRegistry":
        if config is None:
            from src.config import load_config

            config = load_config()
        return cls(config.vendor_registry)

    def learn_from_log(self, entries: list["AuditEntry"]) -> "PayeeRegistry":
        """Add the wallets a payee has already been successfully paid at.

        Only settled successes count. An entry that was escalated, denied,
        refunded, or whose payment failed is not proof that this payee uses this
        wallet -- refunds in particular mean the destination was rejected.
        """
        for entry in entries:
            wallet = normalize_wallet(entry.request.counterparty_wallet)
            if wallet is None:
                continue
            if entry.payment_status not in _PROOF_OF_USE:
                continue
            if entry.human_review == "denied" or entry.escrow_status == "rejected":
                continue
            for party in (entry.request.vendor, entry.request.requester):
                if not party or not party.strip():
                    continue
                self._sources.setdefault(normalize_key(party), {}).setdefault(wallet, "observed")
        return self

    # -- reads -------------------------------------------------------------

    def is_empty(self) -> bool:
        return not self._sources

    def known_wallets(self, payee: str) -> tuple[str, ...]:
        return tuple(sorted(self._sources.get(normalize_key(payee), {})))

    def source_of(self, payee: str, wallet: str) -> str | None:
        return self._sources.get(normalize_key(payee), {}).get(normalize_wallet(wallet) or "")

    def assess(self, payee: str, wallet: str | None) -> PayeeAssessment:
        """Decide whether this payee may be paid at this wallet.

        `payee` is the vendor when the request names one, otherwise the
        requester: whoever the money is going to and being invoiced by.
        """
        key = normalize_key(payee) if payee else ""
        normalized = normalize_wallet(wallet)
        known = self.known_wallets(key) if key else ()

        if not key:
            return PayeeAssessment(
                PayeeStatus.UNKNOWN, payee, normalized, (),
                "the request names no payee to check against the registry",
            )
        if not known:
            return PayeeAssessment(
                PayeeStatus.UNKNOWN, key, normalized, (),
                f"{payee!r} is not in the registry; a human confirms a first payment",
            )
        if normalized is None:
            return PayeeAssessment(
                PayeeStatus.UNKNOWN, key, None, known,
                f"the request names no wallet, but {payee!r} is registered at {len(known)}",
            )
        if normalized in known:
            source = self.source_of(key, normalized)
            return PayeeAssessment(
                PayeeStatus.KNOWN, key, normalized, known,
                f"wallet is registered for {payee!r} ({source})",
            )
        return PayeeAssessment(
            PayeeStatus.MISMATCH, key, normalized, known,
            f"{payee!r} is registered at {len(known)} other wallet(s); this request pays a "
            "different one, which is how a redirected invoice looks",
        )
