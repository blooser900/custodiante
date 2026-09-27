"""Shared rubrics for the trust / urgency / importance questions.

Used by every DecisionEngine implementation (Jev, Kev, or any other
TypeSafe-System-One-compatible backend) so all of them are scored against
the exact same wording, and are therefore comparable.
"""

# Each rubric is an ordered list of level descriptions, low to high.
# For an N-level rubric, the raw Score answer lands in [0, N - 1].
TRUST_CRITERIA = [
    "Likely fraudulent, malformed, or contradicts known facts about the requester",
    "Plausible but has unexplained gaps: vague justification, no supporting detail",
    "Clearly legitimate: well-documented, consistent with the requester's history",
]

URGENCY_CRITERIA = [
    "No real time pressure; could wait weeks with no consequence",
    "Some time pressure; a delay of days would cause friction",
    "Time-critical; missing the deadline causes real, immediate harm",
]

IMPORTANCE_CRITERIA = [
    "Low impact if this payment is never made",
    "Moderate impact: inconvenience or a minor loss of trust/service",
    "High impact: significant financial loss, service outage, or relationship damage",
]


def normalize_score(raw_score: float, num_levels: int) -> float:
    """Map a raw System-One score in [0, num_levels - 1] to [0, 1]."""
    return raw_score / (num_levels - 1)
