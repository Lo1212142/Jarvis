"""Read-only conflict news tool with source provenance."""

from __future__ import annotations

from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.monitoring.conflict_news import ConflictNewsService, ConflictNewsUnavailable, DEFAULT_CONFLICT_QUERY
from openjarvis.tools._stubs import BaseTool, ToolSpec


@ToolRegistry.register("conflict_news")
class ConflictNewsTool(BaseTool):
    tool_id = "conflict_news"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="conflict_news",
            description="Fetch bounded public reports about wars and conflicts, retaining publisher URLs and retrieval time; never use it for targeting or surveillance.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "maxLength": 200}, "refresh": {"type": "boolean"}},
                "additionalProperties": False,
            },
            category="information",
            requires_confirmation=False,
            timeout_seconds=20.0,
            metadata={"external_read": True, "source_provenance_required": True, "operational_targeting": False},
        )

    def execute(self, **params: Any) -> ToolResult:
        query = str(params.get("query") or DEFAULT_CONFLICT_QUERY)
        try:
            snapshot = ConflictNewsService().latest(query=query, force_refresh=bool(params.get("refresh", False)))
            return ToolResult(self.tool_id, str(snapshot.to_dict()), True, metadata={"source_url": snapshot.source_url, "retrieved_at": snapshot.retrieved_at, "stale": snapshot.stale})
        except ConflictNewsUnavailable as exc:
            return ToolResult(self.tool_id, f"Conflict news is unavailable; no current articles were returned: {exc}", False)


__all__ = ["ConflictNewsTool"]
