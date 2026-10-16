"""R2-12: `save_config` must not be able to leave a half-written config behind.

`save_config` opened the target with mode "w", which TRUNCATES before anything
is written, while `load_config` runs on every request (`apply_policy`, the
engine, the API). A kill, an OOM or a full disk inside that window left a
truncated `config/rules.yaml`, and a truncated file is not "the defaults": it
fails `RulesConfig.model_validate`, so EVERY subsequent request would die. The
window is small; the blast radius is the whole system.

The fix is the standard one: write a sibling temporary file, then `os.replace`
it over the target. `os.replace` is atomic on POSIX and on Windows, so a reader
sees either the old file or the new one, never a partial one.
"""

import os
import stat
from pathlib import Path

import pytest

from src.config import RulesConfig, ScoringCriteria, load_config, save_config
from tests.rules_fixtures import category_limits, thresholds


def rules(other_limit: float = 200.0) -> RulesConfig:
    return RulesConfig(
        category_limits=category_limits(other=other_limit),
        thresholds=thresholds(),
        criteria=ScoringCriteria(trust=["a", "b"], urgency=["a", "b"], importance=["a", "b"]),
    )


def sibling_temp_files(path: Path) -> list[Path]:
    """The scratch files an atomic write is allowed to have created.

    Only the target's own directory is inspected: the point of a sibling temp
    file is that it lives on the same filesystem, so `os.replace` cannot fail
    with a cross-device error.
    """
    return [p for p in path.parent.iterdir() if p != path and p.suffix == ".tmp"]


def test_a_crash_during_the_write_leaves_the_previous_config_intact(tmp_path, monkeypatch):
    """The core of R2-12: a failure mid-write must not destroy what was there."""
    import yaml

    path = tmp_path / "rules.yaml"
    save_config(rules(other_limit=200.0), path)
    before = path.read_bytes()
    assert load_config(path).category_limits["other"].max_usdc == 200.0

    # Simulate the process dying between "the file is opened for writing" and
    # "the new content is on disk". With mode "w" the old content is already
    # gone at this point; with an atomic write the target was never touched.
    def exploding_dump(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(yaml, "safe_dump", exploding_dump)

    with pytest.raises(OSError):
        save_config(rules(other_limit=999.0), path)

    assert path.read_bytes() == before, "the previous config must survive a failed write"
    assert load_config(path).category_limits["other"].max_usdc == 200.0
    assert sibling_temp_files(path) == [], "a failed write must not leave scratch files behind"


def test_the_target_is_never_truncated_before_the_new_content_exists(tmp_path, monkeypatch):
    """The specific defect: the target must not be opened for writing at all.

    Asserted on the syscalls rather than on the outcome, because "the file
    happened to survive" is also what a lucky crash gives you. If nothing opens
    the target with a truncating mode, the window cannot exist.
    """
    path = tmp_path / "rules.yaml"
    save_config(rules(), path)

    opened: list[tuple[str, str]] = []
    real_open = Path.open

    def recording_open(self, mode="r", *args, **kwargs):
        opened.append((self.name, mode))
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", recording_open)
    save_config(rules(other_limit=300.0), path)

    truncating = [name for name, mode in opened if name == path.name and "w" in mode]
    assert truncating == [], f"the config was truncated before writing: {opened}"


def test_a_successful_save_still_works_and_leaves_no_scratch(tmp_path):
    """The atomic dance must not change what a normal save does."""
    path = tmp_path / "rules.yaml"

    save_config(rules(other_limit=488.0), path)

    assert load_config(path).category_limits["other"].max_usdc == 488.0
    assert sibling_temp_files(path) == []


def test_the_file_is_replaced_not_rewritten_in_place(tmp_path, monkeypatch):
    """`os.replace` is what makes it atomic; a plain rename or a second open is not."""
    path = tmp_path / "rules.yaml"
    save_config(rules(), path)

    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def recording_replace(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", recording_replace)
    save_config(rules(other_limit=311.0), path)

    assert len(calls) == 1, f"expected exactly one os.replace, got {calls}"
    source, destination = calls[0]
    assert destination == str(path)
    assert Path(source).parent == tmp_path, "the scratch file must be a sibling of the target"


def test_saving_over_an_existing_file_keeps_its_permissions(tmp_path):
    """`os.replace` carries the SCRATCH file's mode, so the save must put the
    target's own mode back instead of leaving the scratch file's.

    A `0o644` config must not come back `0o600` (or vice versa). The Unix bits
    only mean something on POSIX -- Windows `os.chmod` just toggles the
    read-only attribute -- so the bit-for-bit assertion is made where it is real.

    NOT tested here, and worth knowing: on Windows `os.replace` cannot replace a
    file whose read-only attribute is set (`WinError 5`), so saving over a
    read-only config fails there. The previous implementation failed too (mode
    "w" is denied), so this is not a regression, but the failure now happens at
    the replace and the old file is left untouched, which is the safer half.
    """
    path = tmp_path / "rules.yaml"
    save_config(rules(), path)

    if os.name == "posix":
        os.chmod(path, 0o644)
        save_config(rules(other_limit=644.0), path)
        assert path.stat().st_mode & 0o777 == 0o644
    else:
        # The scratch file is created 0o600 and the target's mode is copied onto
        # it; on Windows that assignment is what keeps a writable file writable.
        before = path.stat().st_mode
        save_config(rules(other_limit=644.0), path)
        assert (path.stat().st_mode & stat.S_IWRITE) == (before & stat.S_IWRITE)

    assert load_config(path).category_limits["other"].max_usdc == 644.0


def test_saving_into_a_directory_that_does_not_exist_yet_still_works(tmp_path):
    """First run on a fresh checkout: the parent directory is created for you."""
    path = tmp_path / "config" / "rules.yaml"

    save_config(rules(), path)

    assert path.exists()
    assert load_config(path).category_limits["other"].max_usdc == 200.0
