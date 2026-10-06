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

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "rules.yaml"

# Defaults -- these are the exact values this project used when the rules
# were hardcoded in policy/limits.py, policy/rules.py, and
# decision_engine/criteria.py. config/rules.yaml is versioned in the repo and
# ships with these same values, so behavior is unchanged until someone edits it.
DEFAULT_CATEGORY_LIMITS = {
    "contractor_milestone": 2000.0,
    "supply_purchase": 1000.0,
    "operational_expense": 500.0,
    "other": 200.0,
}

DEFAULT_THRESHOLDS = {
    "trust_min_auto_approve": 0.75,
    "trust_min_confidence_for_auto_approve": 0.7,
    "trust_min_to_consider": 0.4,
    "importance_high": 0.7,
    "urgency_high": 0.7,
    "reduced_amount_fraction": 0.5,
}

DEFAULT_CRITERIA = {
    "trust": [
        "Likely fraudulent, malformed, or contradicts known facts about the requester",
        "Plausible but has unexplained gaps: vague justification, no supporting detail",
        "Clearly legitimate: well-documented, consistent with the requester's history",
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


class Thresholds(BaseModel):
    trust_min_auto_approve: float = Field(ge=0, le=1)
    trust_min_confidence_for_auto_approve: float = Field(ge=0, le=1)
    trust_min_to_consider: float = Field(ge=0, le=1)
    importance_high: float = Field(ge=0, le=1)
    urgency_high: float = Field(ge=0, le=1)
    reduced_amount_fraction: float = Field(gt=0, le=1)


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
    category_limits: dict[str, float]
    thresholds: Thresholds
    criteria: ScoringCriteria


def _default_config() -> RulesConfig:
    return RulesConfig(
        category_limits=dict(DEFAULT_CATEGORY_LIMITS),
        thresholds=Thresholds(**DEFAULT_THRESHOLDS),
        criteria=ScoringCriteria(**DEFAULT_CRITERIA),
    )


def load_config(path: Path = CONFIG_PATH) -> RulesConfig:
    """Load the rules config.

    config/rules.yaml is versioned, so it is present in any normal checkout and
    this function never writes a file -- reading the rules happens on every
    request, and a read must not have filesystem side effects. If the file is
    missing anyway (deleted by hand), fall back to the in-code defaults in
    memory rather than recreating it. Writing goes through save_config(), which
    only scripts/manage_rules.py and the settings page call.
    """
    if not path.exists():
        return _default_config()

    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return RulesConfig.model_validate(raw)


def save_config(config: RulesConfig, path: Path = CONFIG_PATH) -> None:
    """Write the config back to YAML. Note: this does not preserve manual
    comments in an existing file -- it's a full rewrite, not a patch.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(
            config.model_dump(), f, sort_keys=False, allow_unicode=True, width=100
        )
