from pathlib import Path
import time

from openjarvis.monitoring.log_center import LogCenter
from openjarvis.monitoring.log_watcher import LogWatcher


def test_log_watcher_reads_only_new_lines_and_redacts(tmp_path: Path):
    log = tmp_path / "service.log"
    log.write_text("INFO first token=abc\n", encoding="utf-8")
    center = LogCenter(tmp_path)
    center.register_source("service", log)
    watcher = LogWatcher(center, min_interval_seconds=0.05)
    first = watcher.poll("service")
    assert first.lines == ["INFO first token=[REDACTED]"]
    log.write_text("INFO first token=abc\nERROR second\n", encoding="utf-8")
    time.sleep(0.06)
    second = watcher.poll("service")
    assert second.lines == ["ERROR second"]
    throttled = watcher.poll("service")
    assert throttled.lines == []


def test_log_watcher_reset_replays_file(tmp_path: Path):
    log = tmp_path / "service.log"
    log.write_text("INFO first\n", encoding="utf-8")
    center = LogCenter(tmp_path)
    center.register_source("service", log)
    watcher = LogWatcher(center, min_interval_seconds=0.05)
    watcher.poll("service")
    watcher.reset("service")
    time.sleep(0.06)
    assert watcher.poll("service").lines == ["INFO first"]
