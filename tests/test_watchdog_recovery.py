from pathlib import Path

import pytest

from openjarvis.recovery.watchdog import RecoveryCore


def test_recovery_restores_from_baseline_and_is_bounded(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    state = tmp_path / "state"
    target = tmp_path / "target"
    baseline.mkdir()
    (baseline / "config.toml").write_text("safe = true\n", encoding="utf-8")
    core = RecoveryCore(baseline_dir=baseline, state_dir=state, max_recovery_attempts=1)
    result = core.restore_file("config.toml", target)
    assert result.restored is True
    assert (target / "config.toml").read_text() == "safe = true\n"
    second = core.restore_file("config.toml", target)
    assert second.restored is False
    assert "limit" in second.reason


def test_recovery_rejects_traversal(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    state = tmp_path / "state"
    baseline.mkdir()
    (baseline / "safe.txt").write_text("ok", encoding="utf-8")
    core = RecoveryCore(baseline_dir=baseline, state_dir=state)
    with pytest.raises(ValueError):
        core.restore_file("../safe.txt", tmp_path / "target")
