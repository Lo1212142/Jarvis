from pathlib import Path

import pytest

from openjarvis.monitoring.log_center import LogCenter


def test_log_center_redacts_and_analyzes_incident(tmp_path: Path):
    log = tmp_path / "service.log"
    log.write_text("INFO started token=secret123\nERROR request timed out\nWARNING retry\n", encoding="utf-8")
    center = LogCenter(tmp_path)
    source = center.register_source("service", log)
    assert source.path == "service.log"
    rows = center.read("service", contains="secret")
    assert rows[0]["text"] == "INFO started token=[REDACTED]"
    tail = center.tail("service")
    assert "[REDACTED]" in "\n".join(tail["lines"])
    incident = center.incident("service")
    assert incident["error_count"] == 1
    assert "timeout" in incident["hypothesis"]


def test_log_center_blocks_outside_paths_and_unknown_source(tmp_path: Path):
    center = LogCenter(tmp_path / "allowed")
    outside = tmp_path / "outside.log"
    outside.write_text("ERROR secret", encoding="utf-8")
    with pytest.raises(ValueError, match="outside"):
        center.register_source("outside", outside)
    with pytest.raises(ValueError, match="unknown"):
        center.read("missing")
