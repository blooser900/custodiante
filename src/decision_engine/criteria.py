"""Score normalization for System-One decision engines.

The actual rubric text (trust/urgency/importance level descriptions) used
to live here as hardcoded constants. It now lives in config/rules.yaml,
loaded via src.config.load_config() -- see jev_client.py and
fallback_classifier.py, which build their `questions` payload from it.
"""


def normalize_score(raw_score: float, num_levels: int) -> float:
    """Map a raw System-One score in [0, num_levels - 1] to [0, 1]."""
    return raw_score / (num_levels - 1)
