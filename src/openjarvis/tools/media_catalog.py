"""Movie and series discovery tool with provenance and spoiler-safe defaults."""

from __future__ import annotations

from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.media.catalog import MediaCatalogService, MediaCatalogUnavailable
from openjarvis.tools._stubs import BaseTool, ToolSpec


@ToolRegistry.register("media_catalog")
class MediaCatalogTool(BaseTool):
    tool_id = "media_catalog"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="media_catalog",
            description="Search movies and series, offer candidates and optional spoiler-controlled summaries with provider and retrieval time.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 160},
                    "media_type": {"type": "string", "enum": ["all", "movie", "series"]},
                    "language": {"type": "string", "maxLength": 32},
                    "include_summary": {"type": "boolean", "description": "Only true when the user explicitly requests a summary; treat as spoiler-capable."},
                    "refresh": {"type": "boolean"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            category="information",
            requires_confirmation=False,
            timeout_seconds=20.0,
            metadata={"external_read": True, "spoiler_safe_default": True, "source_provenance_required": True},
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            service = MediaCatalogService()
            result = service.search(str(params.get("query", "")), media_type=str(params.get("media_type", "all")), language=str(params.get("language", "en-US")), include_summary=bool(params.get("include_summary", False)), force_refresh=bool(params.get("refresh", False)))
            return ToolResult(self.tool_id, str(result.to_dict(include_summary=bool(params.get("include_summary", False)))), True, metadata={"retrieved_at": result.retrieved_at, "providers": list(result.source_providers), "spoilers": bool(params.get("include_summary", False))})
        except (ValueError, MediaCatalogUnavailable) as exc:
            return ToolResult(self.tool_id, f"Media catalog unavailable or invalid: {exc}", False)


__all__ = ["MediaCatalogTool"]
