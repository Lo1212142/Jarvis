"""Read-only Cairo weather tool with provenance."""

from __future__ import annotations

from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.monitoring.weather import WeatherUnavailable, get_default_weather_service
from openjarvis.tools._stubs import BaseTool, ToolSpec


@ToolRegistry.register("weather_cairo")
class CairoWeatherTool(BaseTool):
    tool_id = "weather_cairo"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="weather_cairo",
            description="Fetch the current Cairo weather from Open-Meteo and report source, retrieval time, and stale state.",
            parameters={"type": "object", "properties": {"refresh": {"type": "boolean"}}, "additionalProperties": False},
            category="information",
            requires_confirmation=False,
            timeout_seconds=15.0,
            metadata={"external_read": True, "truthful_source_required": True},
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            snapshot = get_default_weather_service().current_cairo(force_refresh=bool(params.get("refresh", False)))
            return ToolResult(self.tool_id, str(snapshot.to_dict()), True, metadata={"source_url": snapshot.source_url, "retrieved_at": snapshot.retrieved_at, "stale": snapshot.stale})
        except WeatherUnavailable as exc:
            return ToolResult(self.tool_id, f"Weather is unavailable; no current Cairo observation was returned: {exc}", False)


__all__ = ["CairoWeatherTool"]
