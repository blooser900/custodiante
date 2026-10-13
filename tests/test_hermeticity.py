"""Hermeticity of the suite itself (SEV-04).

These are tests about the tests: running the suite must not write to the
developer's working tree. `load_config()` creates `config/rules.yaml` with
defaults when the file is missing, and several tests legitimately ask for
those defaults -- so without the `isolate_config_file` fixture in conftest.py
the suite regenerates a real config file on every run.
"""

from pathlib import Path

from src.config import load_config

# The real location, derived from this file rather than imported: importing
# CONFIG_PATH here would run while the conftest fixture is already active and
# would therefore hand back the isolated path.
REAL_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "rules.yaml"


def test_running_the_suite_does_not_create_the_real_config_file():
    """A default-path load_config() must not land in the working tree."""
    load_config()  # would write config/rules.yaml if the fixture were absent
    assert not REAL_CONFIG_PATH.exists(), (
        f"the suite wrote {REAL_CONFIG_PATH}; load_config() must be redirected "
        "to a temporary path by the isolate_config_file fixture"
    )


def test_load_config_still_returns_the_defaults_under_isolation():
    """Redirecting the path must not change what a default read returns."""
    config = load_config()
    assert config.category_limits["contractor_milestone"].max_usdc == 2000.0
    assert len(config.criteria.trust) == 3
    assert config.assessment_questions  # the extras are on by default
