"""Self-healing watcher for the creative suite and self-developed tools.

A small supervisor that keeps the new capabilities healthy:

* **Event-driven repair** — subscribes to ``TOOL_CALL_END``; when a tool
  fails repeatedly (3 consecutive failures), runs the matching repair:
  quarantines broken self-dev modules and restores their baseline,
  repairs corrupted media settings, cleans scratch dirs.
* **Periodic health checks** — a daemon thread verifies ffmpeg
  availability, settings integrity, self-dev module importability and
  disk headroom, recording snapshots through ``recovery.watchdog.
  RecoveryCore`` (so the existing recovery subsystem stays the single
  source of truth).

All repairs are additive and conservative: nothing outside
``~/.openjarvis`` is ever touched, and every action is logged.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from openjarvis.creative import media_settings, runtime
from openjarvis.creative._paths import creative_root, tmp_dir

logger = logging.getLogger(__name__)

_WATCHER: Optional["SelfHealWatcher"] = None
_CONSECUTIVE_FAILURES: Dict[str, int] = {}
_FAILURE_THRESHOLD = 3

# Tools whose failures we watch (creative + self-dev namespace).
_WATCHED_PREFIXES = ("image_edit", "video_edit", "demo_video",
                     "media_image_generate", "media_video_generate",
                     "tech_news", "tutor", "remember_preference",
                     "self_dev")


def _is_watched(tool_name: str) -> bool:
    return any(tool_name == prefix or tool_name.startswith(prefix)
               for prefix in _WATCHED_PREFIXES)


class SelfHealWatcher:
    """Periodic + event-driven health monitor with repair procedures."""

    def __init__(self, *, interval_seconds: float = 120.0) -> None:
        self.interval = max(30.0, interval_seconds)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.repairs: Dict[str, int] = {}
        try:
            from openjarvis.core.paths import get_config_dir
            from openjarvis.recovery.watchdog import RecoveryCore

            self.recovery = RecoveryCore(
                state_dir=get_config_dir() / "self-heal" / "state",
                baseline_dir=get_config_dir() / "self-dev" / "baseline",
            )
        except Exception:
            self.recovery = None
        self._record("watcher", True, "constructed")

    # -- health record ----------------------------------------------------

    def _record(self, service: str, healthy: bool, reason: str = "") -> None:
        logger.debug("self-heal[%s] healthy=%s %s", service, healthy, reason)
        if self.recovery is not None:
            try:
                self.recovery.record_health(service, healthy, reason)
            except Exception:
                pass

    # -- repair procedures --------------------------------------------------

    def repair_media_settings(self) -> bool:
        """Repair corrupted media-settings.json by restoring defaults."""
        try:
            from openjarvis.core.paths import get_config_dir

            path = get_config_dir() / "media-settings.json"
            if path.exists():
                try:
                    json.loads(path.read_text("utf-8") or "{}")
                    return True  # already fine
                except json.JSONDecodeError:
                    broken = path.with_suffix(".json.broken")
                    shutil.copyfile(path, broken)
                    path.unlink()
            # Persist a fresh defaults file so the settings exist again.
            defaults = media_settings.load_settings()
            path.write_text(json.dumps(defaults, indent=2, ensure_ascii=False),
                            "utf-8")
            self.repairs["media_settings"] = self.repairs.get("media_settings", 0) + 1
            self._record("media_settings", True, "repaired from corruption")
            return True
        except Exception as exc:
            self._record("media_settings", False, str(exc)[:200])
            return False

    def repair_self_dev_module(self, name: str) -> bool:
        """Quarantine a broken self-dev tool and restore its baseline."""
        try:
            from openjarvis.creative import self_dev

            tools_dir = self_dev._tools_dir()
            baseline_dir = self_dev._baseline_dir()
            module_path = tools_dir / f"{name}.py"
            baseline = baseline_dir / f"{name}.py"
            if not baseline.exists():
                # No known-good version — drop the broken module.
                if module_path.exists():
                    module_path.rename(module_path.with_suffix(".py.broken"))
                self.repairs[f"self_dev:{name}"] = 1
                self._record(f"self_dev:{name}", True, "quarantined (no baseline)")
                return True
            module_path.write_text(baseline.read_text("utf-8"), "utf-8")
            # Re-import and re-register from the baseline (drop the stale
            # registration first — ToolRegistry rejects duplicate keys).
            try:
                from openjarvis.core.registry import ToolRegistry as _TR

                _TR._entries().pop(name, None)
                sys.modules.pop(f"openjarvis_selfdev_{name}", None)
            except Exception:
                pass
            spec = importlib.util.spec_from_file_location(
                f"openjarvis_selfdev_{name}", module_path
            )
            import sys

            import sys

            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            from openjarvis.core.registry import ToolRegistry
            from openjarvis.tools._stubs import BaseTool

            tool_cls = next(
                obj for obj in vars(module).values()
                if isinstance(obj, type) and issubclass(obj, BaseTool)
                and obj is not BaseTool
            )
            instance = tool_cls()
            if not ToolRegistry.contains(name):
                ToolRegistry.register(name)(tool_cls)
            injected = self_dev.inject_into_running_agents(instance)
            self.repairs[f"self_dev:{name}"] = self.repairs.get(f"self_dev:{name}", 0) + 1
            self._record(f"self_dev:{name}", True,
                         f"restored baseline (injected into {len(injected)} agents)")
            return True
        except Exception as exc:
            self._record(f"self_dev:{name}", False, str(exc)[:200])
            return False

    def clean_scratch(self, *, max_age_hours: float = 24.0) -> int:
        """Remove stale render scratch directories (disk hygiene)."""
        removed = 0
        try:
            cutoff = time.time() - max_age_hours * 3600
            for path in tmp_dir().glob("job-*"):
                try:
                    if path.is_dir() and path.stat().st_mtime < cutoff:
                        shutil.rmtree(path, ignore_errors=True)
                        removed += 1
                except OSError:
                    continue
            if removed:
                self.repairs["scratch_cleanup"] = (
                    self.repairs.get("scratch_cleanup", 0) + removed
                )
        except Exception as exc:
            logger.debug("scratch cleanup failed: %s", exc)
        return removed

    # -- health checks ------------------------------------------------------

    def check_all(self) -> Dict[str, bool]:
        results: Dict[str, bool] = {}
        # ffmpeg reachable?
        try:
            proc = subprocess.run(
                ["ffmpeg", "-version"], capture_output=True, timeout=15
            )
            results["ffmpeg"] = proc.returncode == 0
        except Exception:
            results["ffmpeg"] = False
        # Settings parseable + creative dirs writable?
        try:
            media_settings.load_settings()
            probe = creative_root() / ".healthcheck"
            probe.write_text("ok", "utf-8")
            probe.unlink()
            results["media_settings"] = True
        except Exception:
            results["media_settings"] = False
            self.repair_media_settings()
        # Self-dev modules still importable?
        try:
            from openjarvis.creative import self_dev

            for path in self_dev._tools_dir().glob("*.py"):
                name = path.stem
                try:
                    compile(path.read_text("utf-8"), str(path), "exec")
                    results[f"self_dev:{name}"] = True
                except SyntaxError:
                    results[f"self_dev:{name}"] = False
                    self.repair_self_dev_module(name)
        except Exception:
            pass
        for service, healthy in results.items():
            self._record(service, healthy)
        return results

    # -- event-driven repair --------------------------------------------------

    def on_tool_call_end(self, payload: Dict[str, Any]) -> None:
        try:
            tool = str(payload.get("tool") or "")
            if not _is_watched(tool):
                return
            if bool(payload.get("success")):
                _CONSECUTIVE_FAILURES.pop(tool, None)
                return
            count = _CONSECUTIVE_FAILURES.get(tool, 0) + 1
            _CONSECUTIVE_FAILURES[tool] = count
            if count < _FAILURE_THRESHOLD:
                return
            _CONSECUTIVE_FAILURES.pop(tool, None)
            logger.warning(
                "self-heal: tool '%s' failed %d times — attempting repair",
                tool, count,
            )
            if tool.startswith("self_dev") or ":" in tool:
                self.repair_self_dev_module(tool.split(":", 1)[-1].split(".", 1)[0])
            else:
                # Generic repair: settings + scratch hygiene.
                self.repair_media_settings()
                self.clean_scratch(max_age_hours=1.0)
        except Exception:
            logger.debug("self-heal event handler error", exc_info=True)

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="openjarvis-self-heal", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # First check shortly after boot, then on the regular interval.
        self._stop.wait(20.0)
        while not self._stop.wait(self.interval):
            try:
                self.check_all()
                self.clean_scratch()
            except Exception:
                logger.debug("self-heal cycle error", exc_info=True)


def install_self_heal(app: Optional[Any] = None, *,
                      interval_seconds: float = 120.0) -> SelfHealWatcher:
    """Start the watcher + bus subscription (idempotent)."""
    global _WATCHER
    if _WATCHER is not None:
        return _WATCHER
    watcher = SelfHealWatcher(interval_seconds=interval_seconds)
    _WATCHER = watcher

    bus = getattr(getattr(app, "state", None), "bus", None) if app is not None else None
    if bus is not None:
        try:
            from openjarvis.core.events import EventType

            bus.subscribe(EventType.TOOL_CALL_END, lambda _e, p: watcher.on_tool_call_end(p))
        except Exception as exc:
            logger.debug("self-heal bus subscribe skipped: %s", exc)

    agent = getattr(getattr(app, "state", None), "agent", None) if app is not None else None
    if agent is not None:
        runtime.register_agent("primary", agent)

    watcher.start()
    logger.info("self-heal watcher installed (interval %.0fs)", watcher.interval)
    return watcher


def current_watcher() -> Optional[SelfHealWatcher]:
    return _WATCHER


__all__ = ["SelfHealWatcher", "install_self_heal", "current_watcher"]
