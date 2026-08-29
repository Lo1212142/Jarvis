"""Read-only-to-server audio playback command tools."""

from __future__ import annotations

from typing import Any

from openjarvis.audio.playback import get_default_audio_service
from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec


@ToolRegistry.register("audio_play")
class AudioPlayTool(BaseTool):
    tool_id = "audio_play"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="audio_play",
            description=(
                "Command a registered Windows audio client to play a registered "
                "authorized audio track. The result remains unacknowledged until the client replies."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "client_id": {"type": "string", "minLength": 1},
                    "track_id": {"type": "string", "minLength": 1},
                    "title": {"type": "string"},
                },
                "required": ["client_id", "track_id"],
            },
            category="audio",
            requires_confirmation=False,
            timeout_seconds=5.0,
            metadata={"requires_client_ack": True, "external_output": True},
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            result = get_default_audio_service().play(
                str(params.get("client_id", "")),
                str(params.get("track_id", "")),
                title=str(params.get("title", "")),
            )
            return ToolResult(self.tool_id, "Command queued; the Windows client has not acknowledged playback yet.", True, metadata=result)
        except (KeyError, ValueError, FileNotFoundError, PermissionError) as exc:
            return ToolResult(self.tool_id, f"Playback command was not sent: {exc}", False)


@ToolRegistry.register("audio_control")
class AudioControlTool(BaseTool):
    tool_id = "audio_control"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="audio_control",
            description="Control playback or volume on a registered Windows audio client; report acknowledgement separately.",
            parameters={
                "type": "object",
                "properties": {
                    "client_id": {"type": "string", "minLength": 1},
                    "action": {"type": "string", "enum": ["pause", "resume", "stop", "next", "previous", "volume_up", "volume_down", "set_volume"]},
                    "value": {"type": "integer", "minimum": 0, "maximum": 100},
                },
                "required": ["client_id", "action"],
            },
            category="audio",
            requires_confirmation=False,
            timeout_seconds=5.0,
            metadata={"requires_client_ack": True, "external_output": True},
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            result = get_default_audio_service().control(
                str(params.get("client_id", "")),
                str(params.get("action", "")),
                value=params.get("value"),
            )
            return ToolResult(self.tool_id, "Audio control command queued; client acknowledgement is still pending.", True, metadata=result)
        except (KeyError, ValueError) as exc:
            return ToolResult(self.tool_id, f"Audio control was not sent: {exc}", False)


__all__ = ["AudioPlayTool", "AudioControlTool"]
