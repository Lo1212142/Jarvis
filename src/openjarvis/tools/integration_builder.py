"""Tool for preparing new integrations through the guarded self-development flow."""

from __future__ import annotations

from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec
from openjarvis.self_development.pipeline import IntegrationRequest, prepare_integration


@ToolRegistry.register("integration_builder")
class IntegrationBuilderTool(BaseTool):
    """Prepare a new connector/tool for review without activating it."""

    tool_id = "integration_builder"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="integration_builder",
            description=(
                "Prepare a new API connector or tool from public documentation. "
                "Creates an isolated, auditable workspace and test plan; never "
                "changes production code or activates credentials automatically."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "request": {"type": "string", "description": "Feature to build."},
                    "provider": {"type": "string", "description": "Provider slug, e.g. zoho."},
                    "docs_url": {"type": "string", "description": "Final HTTPS documentation URL."},
                    "capabilities": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Least-privilege operations requested.",
                    },
                    "target": {
                        "type": "string",
                        "enum": ["connector", "tool", "channel"],
                    },
                },
                "required": ["request", "provider", "docs_url"],
            },
            category="development",
            requires_confirmation=True,
            timeout_seconds=30.0,
            required_capabilities=["integration:develop"],
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            req = IntegrationRequest(
                request=str(params.get("request", "")),
                provider=str(params.get("provider", "")),
                docs_url=str(params.get("docs_url", "")),
                requested_capabilities=tuple(
                    str(x) for x in (params.get("capabilities") or [])
                ),
                target=str(params.get("target", "connector")),
            )
            if req.target not in {"connector", "tool", "channel"}:
                raise ValueError("target must be connector, tool, or channel")
            artifact = prepare_integration(req)
            return ToolResult(
                tool_name=self.tool_id,
                content=(
                    f"Prepared {req.provider} integration workspace at {artifact.workspace}. "
                    "Production activation is blocked pending review."
                ),
                success=True,
                metadata={
                    "workspace": artifact.workspace,
                    "manifest": artifact.manifest,
                    "plan": artifact.plan,
                    "docs_snapshot": artifact.docs_snapshot,
                    "activation": artifact.activation,
                },
            )
        except Exception as exc:
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Integration preparation failed: {type(exc).__name__}: {exc}",
                success=False,
            )


__all__ = ["IntegrationBuilderTool"]
