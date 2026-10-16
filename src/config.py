"""Runtime-configurable business rules.

Everything a business would reasonably want to tune without touching code
lives here: category spending limits, the policy thresholds that decide
auto-approve / reduced-approve / escalate, and the Jev/Kev scoring rubrics
(the level descriptions sent as `criteria` to the decision engine).

Backed by a human-editable YAML file (config/rules.yaml). Read fresh on
every call -- the file is tiny and request volume in this system is low, so
there's no real cost to re-reading it, and edits (via the CLI, the API, or
by hand) take effect immediately with no restart needed.

Adding a brand-new category (not just changing limits for existing ones) is
NOT covered here -- categories are validated by the RequestCategory enum in
src/requests/schema.py, which is a data-model decision, not a tunable rule.
"""

import contextlib
import logging
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from src.requests.schema import RequestCategory

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "rules.yaml"

# Defaults -- these are the exact values this project used when the rules
# were hardcoded in policy/limits.py, policy/rules.py, and
# decision_engine/criteria.py, so behavior is unchanged until someone edits
# config/rules.yaml (which is generated from these defaults on first run).
DEFAULT_CATEGORY_LIMITS = {
    "contractor_milestone": 2000.0,
    "supply_purchase": 1000.0,
    "operational_expense": 500.0,
    "other": 200.0,
}

DEFAULT_THRESHOLDS = {
    # NOTE: there is deliberately no `trust_min_confidence_for_auto_approve`.
    # The auto-approve rule used to require `trust.confidence >= 0.7`, and the
    # calibration run showed that bar is unreachable for this engine: over 39
    # samples the confidence of `trust` never exceeded 0.60, so the condition
    # never fired and the only effect was halving the legitimate auto-approvals
    # (0/12 good -> 6/12) while blocking no bad case at all. The score itself is
    # what discriminates. See docs/revisao-priorizada.md.
    "trust_min_auto_approve": 0.75,
    "trust_min_to_consider": 0.4,
    "importance_high": 0.7,
    "urgency_high": 0.7,
    "reduced_amount_fraction": 0.5,
    # Probability of fraud at which the machine stops approving anything by
    # itself, whatever the trust score says. See src/policy/rules.py.
    "fraud_veto_at": 0.7,
}

# The extra question the fraud veto reads. It must stay a `noul` question:
# retyping it would silently disable the veto, so RulesConfig refuses that. The
# question being absent means "no veto", which is only safe to do on purpose.
FRAUD_QUESTION_ID = "fraud_indicators"

# The second, independent path to the same veto: the transaction-nature
# question offers `risco_fraude` as an explicit choice, and picking it must stop
# an automatic approval on its own. Reading only the probability left a signal
# that existed in every answer and was never consulted.
TRANSACTION_NATURE_QUESTION_ID = "natureza_transacao"
FRAUD_CHOICE_OPTION = "risco_fraude"

DEFAULT_CRITERIA = {
    "trust": [
        "Provavelmente fraudulento, malformado, ou contradiz fatos conhecidos sobre o solicitante",
        "Plausível mas com lacunas não explicadas: justificativa vaga, sem detalhe de suporte",
        "Claramente legítimo: bem documentado e consistente com o histórico do solicitante",
    ],
    "urgency": [
        "No real time pressure; could wait weeks with no consequence",
        "Some time pressure; a delay of days would cause friction",
        "Time-critical; missing the deadline causes real, immediate harm",
    ],
    "importance": [
        "Low impact if this payment is never made",
        "Moderate impact: inconvenience or a minor loss of trust/service",
        "High impact: significant financial loss, service outage, or relationship damage",
    ],
}


# Questions with fixed meaning: the policy layer reads these three by name.
CORE_QUESTION_IDS = ("trust", "urgency", "importance")
# The System One API accepts at most 10 questions per request.
MAX_QUESTIONS = 10
_QUESTION_ID = re.compile(r"^[a-z][a-z0-9_]*$")


class QuestionSpec(BaseModel):
    """One typed question for the decision engine, as the API expects it.

    Three kinds, each with its own `criteria` shape:
      noul    a yes/no question; the answer is the probability of yes.
              No criteria.
      choice  pick one option; `criteria` maps each option to a description
              of when it applies. Include an "other" option for open-ended
              classification.
      score   position on an ordered scale; `criteria` lists 2-10 level
              descriptions. Describe situations, not degrees.

    TypeSafe recommends one factor per question: split a judgment that
    weighs several factors into separate questions instead of one compound
    question.
    """

    type: Literal["noul", "choice", "score"]
    instructions: str = Field(min_length=1)
    criteria: list[str] | dict[str, str] | None = None

    @model_validator(mode="after")
    def _criteria_match_type(self) -> "QuestionSpec":
        if self.type == "noul" and self.criteria is not None:
            raise ValueError("a noul question takes no criteria")
        if self.type == "choice":
            if not isinstance(self.criteria, dict) or not (2 <= len(self.criteria) <= 255):
                raise ValueError("a choice question needs criteria: a mapping of 2+ options to descriptions")
        if self.type == "score":
            if not isinstance(self.criteria, list) or not (2 <= len(self.criteria) <= 10):
                raise ValueError("a score question needs criteria: a list of 2 to 10 level descriptions")
        return self

    def to_api(self) -> dict:
        question: dict = {"type": self.type, "instructions": self.instructions}
        if self.criteria is not None:
            question["criteria"] = self.criteria
        return question


# Questions asked alongside the three core ones. The set below is the antifraud
# payload under calibration: three questions that overlap deliberately (a
# `choice` that can name fraud outright, a `noul` probability of fraud, and a
# `noul` on whether the description justifies the category), so the policy has
# more than one independent path to a fraud signal.
#
# All of them are in Portuguese on purpose -- the rubric text is read by the
# model, and it is the same language as the request descriptions it must judge.
DEFAULT_ASSESSMENT_QUESTIONS = {
    "natureza_transacao": {
        "type": "choice",
        "instructions": "Qual é a natureza desta transação, considerando o histórico financeiro do solicitante e do destino?",
        "criteria": {
            "compra_padrao": "Transação comum e plenamente compatível com o padrão de gasto deste solicitante.",
            "assinatura_recorrente": "Pagamento de serviço contínuo ou assinatura.",
            "transferencia_atipica": "Movimentação incomum pelo valor, pelo destino ou pelo momento.",
            "risco_fraude": "Forte indício de fraude, destino anômalo ou violação de segurança.",
        },
    },
    "fraud_indicators": {
        "type": "noul",
        "instructions": "Este pedido mostra sinais de fraude, engenharia social, ou destino manipulado?",
    },
    "description_matches_category": {
        "type": "noul",
        "instructions": "A descrição justifica plausivelmente um pagamento na categoria declarada?",
    },
}


class Thresholds(BaseModel):
    trust_min_auto_approve: float = Field(ge=0, le=1)
    trust_min_to_consider: float = Field(ge=0, le=1)
    importance_high: float = Field(ge=0, le=1)
    urgency_high: float = Field(ge=0, le=1)
    reduced_amount_fraction: float = Field(gt=0, le=1)
    # Probability of fraud that vetoes any automatic approval.
    fraud_veto_at: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _bars_are_in_a_sane_order(self) -> "Thresholds":
        """Reject a self-contradicting risk appetite.

        Every field above is individually inside [0, 1], which is exactly why
        the combination needs checking: `trust_min_to_consider=0.9` with
        `trust_min_auto_approve=0.5` used to load without complaint and left a
        policy where a trust of 0.55 escalates while 0.95 auto-approves -- the
        "always escalate" bar sitting above the "approve alone" bar. An
        operator editing the rules through the dashboard got no warning.

        Deliberately NOT checked here: `importance_high`/`urgency_high` against
        `trust_min_to_consider`. The defaults have 0.7 against 0.4, and that is
        coherent -- `trust_min_to_consider` is a hard stop applied first, so a
        request reaching the reduced-approval branch has already cleared 0.4.
        The two thresholds answer different questions and need not be ordered.
        """
        if self.trust_min_to_consider > self.trust_min_auto_approve:
            raise ValueError(
                "trust_min_to_consider must be <= trust_min_auto_approve: a request "
                "that cannot be considered must not also qualify for auto-approval "
                f"(got {self.trust_min_to_consider} > {self.trust_min_auto_approve})"
            )
        return self


class CategoryLimit(BaseModel):
    """The spending ceiling for one category, and whether a part payment makes sense.

    `allow_reduced` exists because the reduced-approval branch pays a *fraction*
    of the ceiling. That is reasonable for a supply purchase -- half the toner
    is still toner -- and meaningless for a contract milestone, where half the
    agreed price is not half a delivery: it is a breach of the contract, or a
    payment for work that was never accepted. So the category decides, and the
    policy escalates instead of pretending a partial payment works.

    Defaults to True so an existing config keeps behaving as it did; only
    categories that must be paid whole need to say so.
    """

    max_usdc: float
    allow_reduced: bool = True

    @field_validator("max_usdc")
    @classmethod
    def _usable_ceiling(cls, v: float) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError(f"max_usdc must be a finite number, got {v}")
        if v <= 0:
            raise ValueError(
                f"max_usdc must be positive, got {v}; a non-positive ceiling "
                "escalates every request in that category"
            )
        return v


# Categories whose payment cannot sensibly be split. Derived from the defaults
# in `_default_category_limits`, kept as a named constant so the reason is not
# buried in a dict.
CATEGORIES_PAID_WHOLE = ("contractor_milestone",)


def _default_category_limits() -> dict[str, CategoryLimit]:
    return {
        name: CategoryLimit(
            max_usdc=amount,
            allow_reduced=name not in CATEGORIES_PAID_WHOLE,
        )
        for name, amount in DEFAULT_CATEGORY_LIMITS.items()
    }


class VendorRegistration(BaseModel):
    """One payee and the wallets it is allowed to be paid at.

    This is the registry the audit data cannot provide. `history.py` keys the
    destination history by the wallet *in the request*, so a wallet that has
    been paid cleanly before looks established -- even when it was never the
    wallet of the vendor being invoiced. Measured on real answers: a request
    that redirected an established contractor's payment to a different account
    scored trust 0.806 (above the 0.75 auto-approve bar) and P(fraud) 0.362
    (far below the veto), and was approved without a human.

    The check that catches it is not a model judgement, it is an equality: the
    wallet in the request must be one this payee is registered to use.
    """

    # The payee as it is typed on requests. Matching is case-insensitive and
    # whitespace-trimmed, the same normalization history.py uses for requesters.
    key: str = Field(min_length=1)
    # Every wallet this payee may be paid at. More than one is legitimate (a
    # vendor billing in two currencies, or a deliberate migration confirmed by
    # a manager) -- which is why a change is an escalation, not a refusal.
    wallets: list[str] = Field(min_length=1)
    # Why these wallets, for whoever reads the log later.
    note: str | None = None
    # Who registered it, and when. Both optional so a hand-written config file
    # stays readable.
    registered_by: str | None = None
    registered_at: str | None = None

    @field_validator("wallets")
    @classmethod
    def _normalized_wallets(cls, v: list[str]) -> list[str]:
        cleaned = sorted({w.strip().lower() for w in v if w and w.strip()})
        if not cleaned:
            raise ValueError("a vendor registration needs at least one non-empty wallet")
        return cleaned

    @field_validator("key")
    @classmethod
    def _normalized_key(cls, v: str) -> str:
        key = v.strip().lower()
        if not key:
            raise ValueError("a vendor registration needs a non-empty key")
        return key


class ScoringCriteria(BaseModel):
    trust: list[str]
    urgency: list[str]
    importance: list[str]

    @field_validator("trust", "urgency", "importance")
    @classmethod
    def _valid_level_count(cls, v: list[str]) -> list[str]:
        # Jev/Kev's Score question type requires between 2 and 10 levels
        # (see docs.typesafe.ai/api) -- enforced here so a bad edit fails
        # fast with a clear message instead of a confusing error from the
        # decision engine's API later.
        if not (2 <= len(v) <= 10):
            raise ValueError(f"must have between 2 and 10 levels, got {len(v)}")
        return v


class RulesConfig(BaseModel):
    category_limits: dict[str, CategoryLimit]
    thresholds: Thresholds
    criteria: ScoringCriteria
    # Absent from older config files: they then get the defaults. Set to {}
    # to ask only the three core questions.
    assessment_questions: dict[str, QuestionSpec] = Field(
        default_factory=lambda: {
            k: QuestionSpec.model_validate(v) for k, v in DEFAULT_ASSESSMENT_QUESTIONS.items()
        }
    )
    # Payees and the wallets they may be paid at. Empty by default, which does
    # NOT switch the check off: an empty registry means every payee is unknown,
    # so the first payment to each one is confirmed by a person and the audit
    # log teaches the registry from there (see `src/payees.py`).
    vendor_registry: dict[str, VendorRegistration] = Field(default_factory=dict)
    # The payee check is on unless a manager says otherwise, explicitly. It used
    # to disable itself whenever the registry was empty, which exempted the
    # first payment of a fresh install (and, because a failed or unconfirmed
    # payment teaches the registry nothing, every payment until one succeeded)
    # while the second payee already needed a human. False exists for demos and
    # for rolling the check out to a system with history; `manage_rules.py
    # payee-check off` is the way to set it, and it is shown in `show`.
    require_known_payee: bool = True

    @field_validator("category_limits", mode="before")
    @classmethod
    def _accept_bare_amounts(cls, v):
        """Accept `operational_expense: 500.0` as well as the full mapping.

        The config file is hand-edited and was a plain `category: amount` map
        before `allow_reduced` existed. Requiring every existing file to be
        rewritten would be a migration nobody asked for, so a bare number is
        read as "this ceiling, and whatever `allow_reduced` this category gets
        by default" -- NOT as "part payments allowed", which would have quietly
        enabled splitting a contractor milestone for every existing config.
        """
        if not isinstance(v, dict):
            return v
        return {
            name: (
                {"max_usdc": amount, "allow_reduced": name not in CATEGORIES_PAID_WHOLE}
                if isinstance(amount, (int, float))
                else amount
            )
            for name, amount in v.items()
        }

    @field_validator("category_limits")
    @classmethod
    def _every_category_has_a_usable_limit(cls, v: dict[str, CategoryLimit]) -> dict[str, CategoryLimit]:
        """A missing or non-positive ceiling silently becomes a 200 USDC default.

        `policy/limits.py` falls back to `limits.get("other", 200.0)`, so a
        config that simply omits `contractor_milestone` drops its ceiling from
        2000 to 200 with no error -- and this file is written by the dashboard
        and by `PUT /api/config`, both of which used to accept `{}`. Requiring
        every `RequestCategory` makes that mistake impossible to save.
        """
        if not v:
            raise ValueError(
                "category_limits cannot be empty: every request category needs a ceiling, "
                "otherwise the policy falls back to a 200 USDC default"
            )
        missing = [c.value for c in RequestCategory if c.value not in v]
        if missing:
            raise ValueError(
                f"category_limits is missing a ceiling for {', '.join(sorted(missing))}; "
                "without it the policy silently falls back to a 200 USDC default"
            )
        return v

    @field_validator("vendor_registry")
    @classmethod
    def _registry_keys_are_unique(
        cls, v: dict[str, VendorRegistration]
    ) -> dict[str, VendorRegistration]:
        """Catch two registrations of the same payee under different casing.

        `VendorRegistration.key` is normalized, and the policy looks payees up
        through that normalized form. A mapping whose own keys disagree with the
        keys inside it would make the lookup miss, silently disabling the check.
        """
        for mapping_key, registration in v.items():
            if mapping_key != registration.key:
                raise ValueError(
                    f"vendor_registry key {mapping_key!r} does not match the "
                    f"registration's own key {registration.key!r}; they must be the "
                    "same normalized value or the lookup will miss"
                )
        return v

    @field_validator("assessment_questions")
    @classmethod
    def _valid_question_ids(cls, v: dict[str, QuestionSpec]) -> dict[str, QuestionSpec]:
        for question_id in v:
            if not _QUESTION_ID.match(question_id):
                raise ValueError(f"question id {question_id!r} must be lowercase letters, digits and underscores")
            if question_id in CORE_QUESTION_IDS:
                raise ValueError(f"question id {question_id!r} is reserved for the core questions")
        if len(v) + len(CORE_QUESTION_IDS) > MAX_QUESTIONS:
            raise ValueError(
                f"at most {MAX_QUESTIONS - len(CORE_QUESTION_IDS)} extra questions are allowed "
                f"(the API accepts {MAX_QUESTIONS} per request, {len(CORE_QUESTION_IDS)} are core), got {len(v)}"
            )
        # The fraud veto reads one specific `noul` question. Retyping it would
        # quietly disable a safety control, so refuse rather than ignore.
        fraud = v.get(FRAUD_QUESTION_ID)
        if fraud is not None and fraud.type != "noul":
            raise ValueError(
                f"{FRAUD_QUESTION_ID!r} must be a 'noul' question: the policy reads it as a "
                f"probability of fraud, and a {fraud.type!r} answer would silently disable "
                "that veto"
            )
        return v


def _default_config() -> RulesConfig:
    return RulesConfig(
        category_limits=_default_category_limits(),
        thresholds=Thresholds(**DEFAULT_THRESHOLDS),
        criteria=ScoringCriteria(**DEFAULT_CRITERIA),
    )


def load_config(path: Path = CONFIG_PATH) -> RulesConfig:
    """Load the rules config, creating config/rules.yaml with defaults if
    it doesn't exist yet (so there's always a real file to hand-edit).

    Creating that file is a CONVENIENCE -- it exists so a human has something
    to edit. On a filesystem that cannot be written (a container, Lambda, a
    read-only volume) the write fails, and before this the failure propagated:
    every request that reaches the policy layer, the engine or the API died
    with a `PermissionError`/`FileNotFoundError` instead of running on the
    defaults. The defaults are returned in memory either way; only the
    hand-editability is lost, and that is worth a warning, not an outage.

    A config file that EXISTS but cannot be read or parsed is a different
    matter and still raises: that is a real misconfiguration, and quietly
    substituting the shipped defaults would hide it.
    """
    if not path.exists():
        config = _default_config()
        try:
            save_config(config, path)
        except OSError:
            logger.warning(
                "Could not create %s; running on the in-memory defaults. The rules "
                "cannot be inspected or hand-edited while this filesystem is read-only.",
                path,
                exc_info=True,
            )
        return config

    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return RulesConfig.model_validate(raw)


def save_config(config: RulesConfig, path: Path = CONFIG_PATH) -> None:
    """Write the config back to YAML, atomically.

    Note: this does not preserve manual comments in an existing file -- it's a
    full rewrite, not a patch.

    ATOMIC because `load_config` runs on EVERY request (`apply_policy`, the
    engine, the API) while this file is being replaced. The obvious
    implementation -- `path.open("w")` then dump -- truncates the target before
    writing a byte, so a kill, an OOM or a full disk inside that window leaves a
    truncated file. A truncated file is not "the defaults": it fails
    `RulesConfig.model_validate`, so every subsequent request would die. The
    window is microseconds; the blast radius is the whole system.

    So the new content is written to a scratch file in the SAME directory (same
    filesystem, or `os.replace` would fail with a cross-device error), flushed
    to disk, and then `os.replace`d over the target. `os.replace` is atomic on
    POSIX and on Windows: a concurrent reader sees either the whole old file or
    the whole new one, never a partial one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    # Carry over the existing file's permissions: os.replace keeps the SCRATCH
    # file's mode, so without this a mode-600 config (a deliberate choice on a
    # shared host) would silently become world-readable on the first save.
    existing_mode = path.stat().st_mode & 0o777 if path.exists() else None

    fd, scratch_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                config.model_dump(), f, sort_keys=False, allow_unicode=True, width=100
            )
            # Flush through to the disk BEFORE the replace, so the rename cannot
            # publish a file whose contents are still only in a buffer.
            f.flush()
            os.fsync(f.fileno())
        if existing_mode is not None:
            os.chmod(scratch_name, existing_mode)
        else:
            # A brand-new config: keep it owner-only. It can name wallets, and
            # the process umask may well allow more than that.
            os.chmod(scratch_name, 0o600)
        os.replace(scratch_name, path)
    except BaseException:
        # Nothing was published, so the previous file is still in place. Remove
        # the scratch file so a failed save does not accumulate litter, and let
        # the original error through untouched: it is the actionable one.
        with contextlib.suppress(OSError):
            os.unlink(scratch_name)
        raise
