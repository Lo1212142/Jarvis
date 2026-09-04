"""Agent tools — how Jarvis *contacts* the user.

Three registered tools (standard ``@ToolRegistry.register`` pattern):

* ``notify_user`` — normal-priority push. Reminders, finished jobs,
  digests, answers the user asked to be pinged about. Respects the
  per-device quiet hours; never rings at 3 AM for a "job completed".
* ``alert_user`` — THE urgent contact. High-priority push with sound +
  vibration on the ``jarivs-urgent`` channel; the notification data flags
  open the Jarivs call screen (``incoming`` mode) and speak the message
  through on-device TTS when the app is foregrounded. Bypasses quiet hours
  when the device allows it. Use only for genuinely important events:
  security alerts, resource danger, time-critical asks, or "the user told
  me to reach out when X happens".
* ``mobile_devices_status`` — who can be reached right now, quiet-hours
  state, and last delivery status. Call this first when a notify seems
  to go nowhere.
"""

from __future__ import annotations

import logging
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.mobile.push_registry import get_shared_registry
from openjarvis.mobile.push_sender import get_shared_sender
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)


def _report_text(report: dict) -> str:
    if report.get("error"):
        return f"✗ Not delivered: {report['error']}"
    lines = [f"✓ Push delivered to {report.get('delivered', 0)}"
             f"/{report.get('targeted', 0)} device(s)."]
    if report.get("skipped_quiet"):
        lines.append(f"• {report['skipped_quiet']} device(s) skipped (quiet hours).")
    for result in report.get("results", [])[:5]:
        state = "ok" if result.get("ok") else f"FAILED ({result.get('status', '?')})"
        lines.append(f"• {result.get('device_id', '?')}: {state}")
    return "\n".join(lines)


@ToolRegistry.register("notify_user")
class NotifyUserTool(BaseTool):
    """Send a normal push notification to the user's paired phone(s)."""

    tool_id = "notify_user"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="notify_user",
            description=(
                "Send a push notification to the user's Jarivs mobile app"
                " (works even when the app is closed). Use it to reach the"
                " user with non-urgent information: reminders they asked for,"
                " finished jobs or renders, scheduled digests, answers to"
                " earlier requests, or anything they should see soon."
                " WHEN THE USER ASKS you to message them on their phone —"
                " 'ابعتلي رسالة / ابعتلي على الموبايل / مواعدني / بلغني /"
                " message me / text me / ping me / send it to my phone' —"
                " this is the tool: deliver the message right now."
                " Normal priority respects each device's quiet hours — for"
                " genuinely important events that need attention NOW use"
                " alert_user instead."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string",
                              "description": "Short notification headline, ≤120 chars (e.g. 'Render finished')."},
                    "message": {"type": "string",
                                "description": "The notification body, ≤240 chars. Clear and specific."},
                    "category": {"type": "string",
                                 "enum": ["info", "reminder", "success", "job", "message"],
                                 "default": "info",
                                 "description": "Kind of update (shown in the app)."},
                    "priority": {"type": "string", "enum": ["normal", "high"],
                                 "default": "normal",
                                 "description": "Delivery priority (high still respects quiet hours)."},
                    "action": {"type": "string", "enum": ["chat", "operations", "discovery", "settings"],
                               "description": "Screen to open when the notification is tapped."},
                    "speak": {"type": "boolean", "default": False,
                              "description": "Speak the message on the device when the app is open."},
                },
                "required": ["title", "message"],
            },
            category="communication",
            timeout_seconds=20.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        title = str(params.get("title") or "").strip()
        message = str(params.get("message") or params.get("body") or "").strip()
        if not title and not message:
            return ToolResult(tool_name="notify_user",
                              content="No title or message provided — nothing sent.",
                              success=False)
        sender = get_shared_sender()
        data = {"category": str(params.get("category") or "info"),
                "speak": bool(params.get("speak"))}
        if params.get("action"):
            data["route"] = str(params["action"])
        report = sender.notify(
            title=title or "Jarivs", body=message, data=data,
            urgent=False,
            sound="default" if str(params.get("priority") or "normal") == "high" else None)
        return ToolResult(tool_name="notify_user", content=_report_text(report),
                          success=bool(report.get("delivered")),
                          metadata=report)


@ToolRegistry.register("alert_user")
class AlertUserTool(BaseTool):
    """URGENT contact — Jarvis *calls* the user's phone."""

    tool_id = "alert_user"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="alert_user",
            description=(
                "URGENT: contact the user immediately. Sends a high-priority"
                " push with sound and vibration on the jarivs-urgent channel;"
                " tapping it opens the Jarivs call screen and the message is"
                " spoken on the device. Bypasses quiet hours (when the device"
                " allows). Use ONLY when something truly important needs the"
                " user NOW — a security alert, server resource danger, a"
                " time-critical event, or the user explicitly asked"
                " 'اتصل عليا / كلمني / ناديني / رن عليا / call me / ring me /"
                " reach me now' — or 'كلمني لو حصل كذا' and it happened."
                " This is Jarvis's way of calling the user, not a louder"
                " notification: do not use it for routine updates."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "reason": {"type": "string",
                               "description": "One-line why this is urgent (e.g. 'CPU 95% for 10 min')."},
                    "message": {"type": "string",
                                "description": "What Jarvis says when it calls, ≤240 chars."},
                    "call": {"type": "boolean", "default": True,
                             "description": "Open the incoming-call screen on tap (true) or a plain alert."},
                    "speak": {"type": "boolean", "default": True,
                              "description": "Speak the message out loud on the device when possible."},
                },
                "required": ["reason", "message"],
            },
            category="communication",
            timeout_seconds=25.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        reason = str(params.get("reason") or "").strip()
        message = str(params.get("message") or "").strip()
        if not message:
            return ToolResult(tool_name="alert_user",
                              content="No message provided — nothing sent.",
                              success=False)
        sender = get_shared_sender()
        data = {"category": "urgent",
                "jarvisUrgent": True,
                "speak": bool(params.get("speak", True))}
        if bool(params.get("call", True)):
            data["jarvisCall"] = True
        report = sender.notify(title=f"⚠ Jarvis: {reason or 'Important'}",
                               body=message, data=data, urgent=True)
        content = _report_text(report)
        if not report.get("targeted"):
            content += ("\nNo device could be reached urgently. The user has no"
                        " push-registered phone — ask them to open the Jarivs"
                        " app once so it registers its push token.")
        return ToolResult(tool_name="alert_user", content=content,
                          success=bool(report.get("delivered")),
                          metadata=report)


@ToolRegistry.register("mobile_devices_status")
class MobileDevicesStatusTool(BaseTool):
    """List push-reachable devices and their current state."""

    tool_id = "mobile_devices_status"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="mobile_devices_status",
            description=(
                "Check how the user can be reached right now: which paired"
                " devices have live push tokens, whether any is in quiet"
                " hours, and the last delivery status per device. Call this"
                " when notify_user/alert_user reports no reachable device or"
                " you are unsure whether the user can be contacted."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            category="communication",
            timeout_seconds=10.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        registry = get_shared_registry()
        from openjarvis.mobile.push_registry import in_quiet_hours, minutes_of_day

        records = registry.all_records()
        if not records:
            return ToolResult(tool_name="mobile_devices_status",
                              content=("No push devices registered. The user must open"
                                       " the Jarivs app once after the mobile update"
                                       " so the phone registers its push token."),
                              success=True,
                              metadata=registry.stats())
        now_min = minutes_of_day()
        lines = [f"{len(records)} paired push device(s):"]
        for record in records:
            quiet = in_quiet_hours(record.get("quiet_start_minutes"),
                                   record.get("quiet_end_minutes"), now_min)
            state = "reachable" if record.get("enabled") else "disabled"
            if quiet:
                state += " · quiet hours"
            if record.get("urgent_bypass") and quiet:
                state += " (urgent breaks through)"
            lines.append(f"• {record.get('device_name') or record['device_id']}"
                         f" — {state}; last: {record.get('last_status')}"
                         f" ({record.get('delivered', 0)} ok /"
                         f" {record.get('failed', 0)} failed)")
        return ToolResult(tool_name="mobile_devices_status",
                          content="\n".join(lines), success=True,
                          metadata=registry.stats())


__all__ = ["NotifyUserTool", "AlertUserTool", "MobileDevicesStatusTool"]
