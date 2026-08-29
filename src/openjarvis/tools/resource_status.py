"""Read-only truthful resource status tool."""

from __future__ import annotations

from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.monitoring.resource_monitor import ResourceMonitor, snapshot_json
from openjarvis.tools._stubs import BaseTool, ToolSpec


@ToolRegistry.register("resource_status")
class ResourceStatusTool(BaseTool):
    """Return measured Jarvis process and host resource usage."""

    tool_id = "resource_status"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="resource_status",
            description=(
                "Read the current measured CPU and RAM usage of the Jarvis server "
                "process and host. Never infer or fabricate missing measurements."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            category="system",
            requires_confirmation=False,
            timeout_seconds=5.0,
            metadata={"truthful_measurement": True, "read_only": True},
        )

    def execute(self, **params: Any) -> ToolResult:
        del params
        monitor = ResourceMonitor(poll_interval_seconds=60.0)
        snapshot = monitor.sample()
        if not snapshot.measurement_available:
            return ToolResult(
                tool_name=self.tool_id,
                content=(
                    "يا Boss، معرفتش أقيس استهلاك CPU/RAM حاليًا لأن نظام التشغيل "
                    "لم يرجع قياسًا موثوقًا. أنا آسف، ومش هخمن رقم."
                ),
                success=False,
                metadata={"measurement_available": False, "snapshot": snapshot.to_dict()},
            )
        return ToolResult(
            tool_name=self.tool_id,
            content=snapshot_json(snapshot),
            success=True,
            metadata={"measurement_available": True, "snapshot": snapshot.to_dict()},
        )


__all__ = ["ResourceStatusTool"]
