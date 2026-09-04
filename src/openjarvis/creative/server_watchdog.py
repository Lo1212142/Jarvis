"""In-process server watchdog — the server's side of self-recovery.

Installed by ``install_creative_routes`` (no extra surgical edits). Four
responsibilities:

* **Heartbeat** — a daemon thread writes ``~/.openjarvis/guardian/
  heartbeat.json`` every 10 seconds. The *external* guardian process
  (``openjarvis.creative.guardian``) reads it to distinguish a live
  server from a hung one (stale heartbeat ⇒ freeze ⇒ external restart).
* **Crash capture** — ``sys.excepthook`` / ``threading.excepthook``
  append unhandled tracebacks to ``~/.openjarvis/guardian/crash-report.log``
  so post-mortems explain *why* the process died, not just that it did.
* **Boot preflight** — before serving traffic, every self-developed
  module is syntax-checked (broken ones are quarantined and their
  baseline restored) and JSON configs are validated. Self-inflicted
  breakage is repaired *before* it can crash the boot.
* **Boot re-registration** — self-developed tools are re-imported,
  re-registered and re-injected after every restart, so tools Jarvis
  built for itself survive reboots (unless a ``DISABLED`` flag file was
  placed by the guardian's recovery mode).

An ``atexit`` hook stamps the heartbeat with ``{"stopping": true}`` on
clean shutdown (SIGTERM/Ctrl-C) so the guardian does not classify an
intentional stop as a crash.
"""

from __future__ import annotations

import atexit
import importlib.util
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_INSTALLED = False
_STARTED_AT = time.time()

_HEARTBEAT_INTERVAL = 10.0
_CRASH_LOG_MAX_BYTES = 200_000

# Known JSON config files that, if corrupted, should be flagged at boot.
_KNOWN_JSON_CONFIGS = ("media-settings.json", "runtime-settings.json")


def guardian_dir() -> Path:
    from openjarvis.core.paths import get_config_dir

    path = get_config_dir() / "guardian"
    path.mkdir(parents=True, exist_ok=True)
    return path


def heartbeat_path() -> Path:
    return guardian_dir() / "heartbeat.json"


def crash_log_path() -> Path:
    return guardian_dir() / "crash-report.log"


# ---------------------------------------------------------------------------
# Crash capture
# ---------------------------------------------------------------------------

def _append_crash(kind: str, text: str) -> None:
    """Append a traceback chunk to the crash report log (size-capped)."""
    try:
        path = crash_log_path()
        if path.exists() and path.stat().st_size > _CRASH_LOG_MAX_BYTES:
            rotated = path.with_suffix(".log.old")
            try:
                rotated.write_text(path.read_text("utf-8")[-_CRASH_LOG_MAX_BYTES // 2:],
                                   "utf-8")
                path.unlink()
            except OSError:
                return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n== {stamp} [{kind}] pid={os.getpid()} ==\n{text.strip()}\n")
    except Exception:
        logger.debug("crash log append failed", exc_info=True)


def _install_excepthooks() -> None:
    prev_sys = sys.excepthook
    prev_thread = threading.excepthook

    def sys_hook(exc_type, exc, tb):
        _append_crash("unhandled-exception",
                      "".join(__import__("traceback").format_exception(exc_type, exc, tb)))
        prev_sys(exc_type, exc, tb)

    def thread_hook(args):
        _append_crash(
            f"unhandled-exception/thread:{getattr(args.thread, 'name', '?')}",
            "".join(__import__("traceback").format_exception(args.exc_type,
                                                             args.exc_value,
                                                             args.exc_traceback)),
        )
        prev_thread(args)

    try:
        sys.excepthook = sys_hook
    except Exception:
        pass
    try:
        threading.excepthook = thread_hook
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def write_heartbeat(extra: Optional[Dict[str, Any]] = None) -> None:
    """Write one heartbeat sample (also used by atexit with stopping=true)."""
    try:
        payload: Dict[str, Any] = {
            "pid": os.getpid(),
            "ts": time.time(),
            "started_at": _STARTED_AT,
            "uptime_s": round(time.time() - _STARTED_AT, 1),
        }
        if extra:
            payload.update(extra)
        tmp = heartbeat_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), "utf-8")
        tmp.replace(heartbeat_path())
    except Exception:
        logger.debug("heartbeat write failed", exc_info=True)


class _HeartbeatThread(threading.Thread):
    def __init__(self) -> None:
        super().__init__(name="openjarvis-heartbeat", daemon=True)
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(_HEARTBEAT_INTERVAL):
            write_heartbeat({"status": "alive"})


def _start_heartbeat() -> None:
    write_heartbeat({"status": "boot"})
    thread = _HeartbeatThread()
    thread.start()
    atexit.register(write_heartbeat, {"status": "stopping", "stopping": True})


# ---------------------------------------------------------------------------
# Boot preflight — repair self-inflicted damage before it can crash boot
# ---------------------------------------------------------------------------

def preflight_check() -> Dict[str, Any]:
    """Validate self-dev modules + JSON configs; repair what's broken.

    Returns a report dict (actions taken). Never raises.
    """
    report: Dict[str, Any] = {"checked": [], "repaired": [], "quarantined": []}
    try:
        from openjarvis.creative import self_dev

        for path in sorted(self_dev._tools_dir().glob("*.py")):
            name = path.stem
            report["checked"].append(name)
            try:
                compile(path.read_text("utf-8"), str(path), "exec")
            except SyntaxError as exc:
                report["quarantined"].append(f"{name}: {exc}")
                self_dev._quarantine(name)
    except Exception as exc:
        logger.debug("preflight self-dev check skipped: %s", exc)

    try:
        from openjarvis.core.paths import get_config_dir

        for rel in _KNOWN_JSON_CONFIGS:
            path = get_config_dir() / rel
            if not path.exists():
                continue
            try:
                json.loads(path.read_text("utf-8") or "{}")
            except (json.JSONDecodeError, OSError) as exc:
                # Corrupted JSON — quarantine it; every loader falls back
                # to defaults when the file is missing.
                broken = path.with_name(f"{rel}.preflight-broken")
                try:
                    broken.write_text(path.read_text("utf-8")[:4000], "utf-8")
                except OSError:
                    pass
                try:
                    path.unlink()
                    report["repaired"].append(f"{rel}: quarantined corrupt JSON ({exc})")
                except OSError:
                    pass
    except Exception as exc:
        logger.debug("preflight config check skipped: %s", exc)
    return report


# ---------------------------------------------------------------------------
# Boot re-registration — self-dev tools survive restarts
# ---------------------------------------------------------------------------

def self_dev_disabled() -> bool:
    try:
        from openjarvis.creative import self_dev

        return (self_dev._self_dev_root() / "DISABLED").exists()
    except Exception:
        return False


def register_self_dev_tools_at_boot(agent: Any = None) -> List[str]:
    """Re-import, re-register and re-inject existing self-dev tools.

    Each module is import-tested in a clean subprocess first (hard
    timeout), so a module that is syntactically valid but hangs or
    raises on import cannot take the server down — it is quarantined
    and its baseline restored instead.
    """
    loaded: List[str] = []
    if self_dev_disabled():
        logger.info("self-dev tools disabled by guardian recovery flag")
        return loaded
    try:
        from openjarvis.core.registry import ToolRegistry
        from openjarvis.creative import self_dev
        from openjarvis.tools._stubs import BaseTool

        for path in sorted(self_dev._tools_dir().glob("*.py")):
            name = path.stem
            try:
                errs = self_dev._import_in_subprocess(path, f"boot_{name}",
                                                      timeout=15.0)
                if errs:
                    self_dev._quarantine(name)
                    logger.warning("self-dev tool '%s' failed boot import "
                                   "(quarantined): %s", name, errs[0])
                    continue
                if ToolRegistry.contains(name):
                    continue  # already registered by an earlier install
                spec = importlib.util.spec_from_file_location(
                    f"openjarvis_selfdev_{name}", path
                )
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                tool_cls = next(
                    obj for obj in vars(module).values()
                    if isinstance(obj, type) and issubclass(obj, BaseTool)
                    and obj is not BaseTool
                )
                ToolRegistry.register(name)(tool_cls)
                injected = self_dev.inject_into_running_agents(tool_cls())
                if agent is not None:
                    runtime = getattr(agent, "_executor", None)
                    if runtime is not None and hasattr(runtime, "_tools"):
                        runtime._tools[name] = tool_cls()
                loaded.append(name)
                logger.info("self-dev tool '%s' re-registered (injected into "
                            "%d agents)", name, len(injected))
            except Exception as exc:
                logger.warning("self-dev boot load '%s' failed: %s", name, exc)
    except Exception as exc:
        logger.debug("self-dev boot registration skipped: %s", exc)
    return loaded


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------

def install_server_watchdog(app: Any = None) -> Dict[str, Any]:
    """Install heartbeat + crash capture + preflight (idempotent)."""
    global _INSTALLED
    installed: Dict[str, Any] = {}
    if _INSTALLED:
        installed["already_installed"] = True
        return installed
    with _LOCK:
        if _INSTALLED:
            installed["already_installed"] = True
            return installed
        _INSTALLED = True

    # 1) Repair self-inflicted damage BEFORE anything else runs.
    installed["preflight"] = preflight_check()

    # 2) Re-register tools Jarvis built for itself (across restarts).
    agent = getattr(getattr(app, "state", None), "agent", None)
    installed["self_dev_tools_loaded"] = register_self_dev_tools_at_boot(agent)

    # 3) Crash forensics.
    _install_excepthooks()
    installed["excepthooks"] = True

    # 4) Liveness heartbeat for the external guardian.
    _start_heartbeat()
    installed["heartbeat"] = True

    logger.info("server watchdog installed: preflight=%s self_dev=%s",
                installed["preflight"].get("repaired")
                or installed["preflight"].get("quarantined")
                or "clean",
                installed["self_dev_tools_loaded"])
    return installed


__all__ = [
    "install_server_watchdog",
    "preflight_check",
    "register_self_dev_tools_at_boot",
    "write_heartbeat",
    "heartbeat_path",
    "crash_log_path",
    "self_dev_disabled",
    "guardian_dir",
]
