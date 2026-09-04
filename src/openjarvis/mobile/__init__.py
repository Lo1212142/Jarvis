"""Mobile companion — the outbound "Jarvis contacts you" channel for OpenJarvis.

A fully additive package (same philosophy as the creative suite) that gives
Jarvis a real way to *reach the user* instead of waiting to be asked:

* **Push delivery** — Expo push (keyless, no account/API key needed) to every
  paired phone that registered a push token. Works while the app is
  backgrounded or killed; survives server restarts (persisted registry).
* **Agent tools** — ``notify_user`` (normal: reminders, results, jobs done),
  ``alert_user`` (URGENT: high-priority push + sound + vibration that opens
  the Jarivs call screen — "Jarvis is calling you"), and
  ``mobile_devices_status`` (who can be reached right now).
* **Proactive watcher** — subscribes to the server's event bus and pushes
  automatically on important events: resource alerts (CPU/RAM), security
  alerts/blocks, failed scheduled tasks, new channel messages. Cooldowns
  prevent spam; quiet hours are respected (urgent alerts may bypass).
* **Mobile API** — ``/api/mobile/*`` endpoints let a paired device register
  its Expo push token, set quiet hours, and test the channel. Device tokens
  are already authenticated by the existing ``AuthMiddleware`` (new
  ``events``-scoped ``/api/mobile`` surface).

Integration is one call: ``install_mobile_routes(app)`` plus one import
block in ``tools/__init__.py`` (see INTEGRATION.md). Zero new Python
dependencies — push delivery uses the standard library.
"""

from __future__ import annotations

from openjarvis.mobile.mobile_routes import install_mobile_routes

__all__ = ["install_mobile_routes"]
