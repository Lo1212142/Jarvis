from __future__ import annotations

import json

from openjarvis.core.types import ToolCall
from openjarvis.monitoring.resource_monitor import ResourceMonitor, ResourceSnapshot
from openjarvis.tools.resource_status import ResourceStatusTool


def _snapshot(*, cpu: float | None = None, memory: float | None = None) -> ResourceSnapshot:
    return ResourceSnapshot(
        timestamp=100.0,
        process_cpu_percent=cpu,
        process_rss_mb=128.0 if memory is not None else None,
        process_memory_percent=memory,
        system_cpu_percent=20.0,
        system_memory_percent=40.0,
        system_memory_available_mb=1000.0,
        cpu_count=4,
        system_memory_total_mb=2048.0,
        measurement_available=cpu is not None or memory is not None,
    )


def test_sample_reports_a_real_or_explicitly_unavailable_measurement() -> None:
    snapshot = ResourceMonitor().sample()
    assert snapshot.measurement_available == (
        snapshot.process_cpu_percent is not None or snapshot.process_rss_mb is not None
    )
    if snapshot.process_rss_mb is not None:
        assert snapshot.process_rss_mb > 0


def test_alerts_are_edge_triggered_and_recover() -> None:
    monitor = ResourceMonitor(cpu_alert_percent=80, memory_alert_percent=80, alert_cooldown_seconds=0)
    monitor._evaluate_alerts(_snapshot(cpu=90, memory=20))  # noqa: SLF001
    monitor._evaluate_alerts(_snapshot(cpu=95, memory=20))  # noqa: SLF001
    assert len(monitor.alerts()) == 1
    monitor._evaluate_alerts(_snapshot(cpu=10, memory=20))  # noqa: SLF001
    monitor._evaluate_alerts(_snapshot(cpu=90, memory=20))  # noqa: SLF001
    assert len(monitor.alerts()) == 2


def test_alert_callback_receives_only_real_threshold_crossings() -> None:
    events: list[dict] = []
    monitor = ResourceMonitor(cpu_alert_percent=80, memory_alert_percent=80, alert_cooldown_seconds=0, alert_callback=events.append)
    monitor._evaluate_alerts(_snapshot(cpu=90, memory=20))  # noqa: SLF001
    monitor._evaluate_alerts(_snapshot(cpu=95, memory=20))  # noqa: SLF001
    assert len(events) == 1
    assert events[0]["kind"] == "cpu"
    assert events[0]["value"] == 90.0


def test_missing_measurement_never_creates_alert() -> None:
    monitor = ResourceMonitor(cpu_alert_percent=1, memory_alert_percent=1)
    monitor._evaluate_alerts(_snapshot(cpu=None, memory=None))  # noqa: SLF001
    assert monitor.alerts() == []


def test_resource_status_returns_json_measurement() -> None:
    result = ResourceStatusTool().execute()
    assert result.metadata["measurement_available"] is True
    payload = json.loads(result.content)
    assert payload["measurement_available"] is True
    assert payload["process_rss_mb"] is not None
