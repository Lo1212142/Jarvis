#!/usr/bin/env python3
"""Jarvis Guardian — external supervisor for whole-process self-recovery.

This is the answer to "what if Jarvis *itself* crashes or freezes?":
code inside a dead process cannot repair anything, so a tiny supervisor
process keeps watch from the outside and heals the system:

* **Crash recovery**   — the server dies (unhandled exception, OOM,
  segfault) ⇒ the guardian captures the stderr tail + exit code into a
  crash report and restarts the server with exponential backoff.
* **Hang recovery**    — the process is alive but frozen (deadlock,
  infinite loop, stuck engine) ⇒ the heartbeat written by the in-server
  watchdog goes stale ⇒ guardian sends SIGTERM, then SIGKILL, restarts.
  An optional TCP port probe works as a fallback liveness check.
* **Boot recovery**    — the server crashes repeatedly during startup ⇒
  the guardian enters *recovery mode*: quarantines broken self-developed
  tools (restoring their baselines), quarantines corrupted JSON configs,
  and (as a last resort) disables self-dev loading entirely via a flag
  file. Then it retries the boot.
* **Crash-loop circuit breaker** — more than N restarts per rolling
  hour ⇒ the guardian parks the service, writes a diagnosis, and waits
  (instead of spinning the CPU); it auto-resumes after the hour or when
  ``--resume`` is invoked.
* **Clean-stop awareness** — a heartbeat stamped ``"stopping": true``
  (written by the server on SIGTERM / Ctrl-C) means an *intentional*
  stop: the guardian exits with it instead of fighting the user.

Deliberately dependency-free and importable without the openjarvis
package — the supervisor must survive the supervised being broken.

Usage:
    python guardian.py                       # supervise `jarvis serve`
    python guardian.py --exec "jarvis serve --host 0.0.0.0"
    python guardian.py --status              # health snapshot, exit 0
    python guardian.py --stop                # graceful stop of guardian+server
    python guardian.py --resume              # clear the circuit breaker
    python guardian.py --recover             # run recovery mode once, exit
    python guardian.py --once                # supervise one run, no restart

Configuration lives in ``~/.openjarvis/guardian/config.json`` (created
with defaults on first run; edit it while stopped). State lives in
``~/.openjarvis/guardian/state.json``.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

VERSION = "1.0.0"

DEFAULTS: Dict[str, Any] = {
    "command": "jarvis serve",
    "cwd": None,
    "boot_window_seconds": 120.0,      # dies before this = boot failure
    "stable_after_seconds": 300.0,     # uptime after which backoff resets
    "heartbeat_stale_seconds": 90.0,   # older than this = hang
    "check_interval_seconds": 5.0,
    "port_probe": None,                # optional {"host":..., "port":...}
    "max_restarts_per_hour": 12,
    "boot_failures_before_recovery": 3,
    "backoff_initial_seconds": 1.0,
    "backoff_max_seconds": 60.0,
    "graceful_termination_seconds": 12.0,
    "restart_on_clean_exit": False,    # user stopped server => stop too
}

# JSON configs that may be quarantined by recovery mode (loaders all
# fall back to defaults when the file is absent).
_RECOVERABLE_JSON_CONFIGS = ("media-settings.json", "runtime-settings.json")

_STDERR_TAIL_LINES = 60


# ---------------------------------------------------------------------------
# Paths (honors OPENJARVIS_HOME, mirroring openjarvis.core.paths)
# ---------------------------------------------------------------------------

def base_dir() -> Path:
    env = os.environ.get("OPENJARVIS_HOME")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / "openjarvis"
    return Path.home() / ".openjarvis"


def guardian_dir() -> Path:
    return base_dir() / "guardian"


def self_dev_dir() -> Path:
    return base_dir() / "self-dev"


def _config_path() -> Path:
    return guardian_dir() / "config.json"


def _state_path() -> Path:
    return guardian_dir() / "state.json"


def _ctl_path() -> Path:
    return guardian_dir() / "ctl.json"


def _heartbeat_path() -> Path:
    return guardian_dir() / "heartbeat.json"


# ---------------------------------------------------------------------------
# Config / state helpers
# ---------------------------------------------------------------------------

def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    try:
        raw = _config_path().read_text("utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def ensure_config() -> Dict[str, Any]:
    cfg = load_config()
    try:
        guardian_dir().mkdir(parents=True, exist_ok=True)
        if not _config_path().exists():
            _config_path().write_text(
                json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")
    except OSError:
        pass
    return cfg


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), "utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _append_log(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n== {stamp} ==\n{text}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Liveness probes
# ---------------------------------------------------------------------------

def heartbeat_is_stale(cfg: Dict[str, Any], now: Optional[float] = None) -> \
        Optional[Dict[str, Any]]:
    """Return the stale heartbeat dict, or None when fresh/absent."""
    hb = _read_json(_heartbeat_path())
    if hb is None or "ts" not in hb:
        return None  # no heartbeat yet — fall back to other probes
    now = now if now is not None else time.time()
    age = now - float(hb.get("ts", 0))
    if age > float(cfg.get("heartbeat_stale_seconds", 90.0)):
        hb["_age_s"] = round(age, 1)
        return hb
    return None


def port_is_down(cfg: Dict[str, Any]) -> bool:
    """Optional TCP liveness probe (True = unreachable)."""
    probe = cfg.get("port_probe")
    if not probe:
        return False
    import socket

    try:
        with socket.create_connection(
            (str(probe.get("host", "127.0.0.1")), int(probe.get("port", 8000))),
            timeout=float(probe.get("timeout", 3.0)),
        ):
            return False
    except OSError:
        return True


def last_heartbeat_says_stopping() -> bool:
    hb = _read_json(_heartbeat_path())
    return bool(hb and hb.get("stopping"))


# ---------------------------------------------------------------------------
# Recovery mode — heals the environment so the next boot can succeed
# ---------------------------------------------------------------------------

def _quarantine(path: Path, suffix: str = ".broken") -> Optional[Path]:
    if not path.exists():
        return None
    dest = path.with_name(path.name + suffix)
    i = 0
    while dest.exists():
        i += 1
        dest = path.with_name(f"{path.name}.broken{i}")
    try:
        path.rename(dest)
        return dest
    except OSError:
        return None


def run_recovery_mode(*, aggressive: bool = False) -> Dict[str, Any]:
    """Quarantine broken self-dev tools + corrupted configs. Never raises.

    ``aggressive=True`` (used after repeated failed recoveries) also
    quarantines *all* self-dev tools and sets the DISABLED flag so the
    server boots without them — Jarvis comes up degraded instead of
    not coming up at all.
    """
    actions: List[str] = []

    tools_dir = self_dev_dir() / "tools"
    baseline_dir = self_dev_dir() / "baseline"
    tools_dir.mkdir(parents=True, exist_ok=True)

    try:
        for path in sorted(tools_dir.glob("*.py")):
            name = path.stem
            if aggressive:
                if _quarantine(path):
                    actions.append(f"aggressive: quarantined {name}.py")
                continue
            broken = True
            try:
                compile(path.read_text("utf-8"), str(path), "exec")
                broken = False
            except (SyntaxError, OSError):
                pass
            if not broken:
                continue
            baseline = baseline_dir / f"{name}.py"
            if baseline.exists():
                if _quarantine(path):
                    try:
                        path.write_text(baseline.read_text("utf-8"), "utf-8")
                        actions.append(f"restored baseline for {name}.py")
                    except OSError:
                        actions.append(f"could not restore baseline for {name}")
            else:
                if _quarantine(path):
                    actions.append(f"quarantined {name}.py (no baseline)")

        if aggressive:
            flag = self_dev_dir() / "DISABLED"
            if not flag.exists():
                flag.write_text(
                    f"disabled by guardian recovery at "
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n", "utf-8")
                actions.append("set self-dev DISABLED flag")
    except OSError as exc:
        actions.append(f"self-dev recovery error: {exc}")

    for rel in _RECOVERABLE_JSON_CONFIGS:
        path = base_dir() / rel
        if not path.exists():
            continue
        try:
            json.loads(path.read_text("utf-8") or "{}")
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            if _quarantine(path, suffix=".corrupt"):
                actions.append(f"quarantined corrupt {rel} ({str(exc)[:60]})")

    # Stale heartbeat from the previous run would instantly look like a
    # hang; clear it so the new boot gets a clean slate.
    try:
        _heartbeat_path().unlink(missing_ok=True)
        actions.append("cleared stale heartbeat")
    except OSError:
        pass

    if actions:
        _append_log(guardian_dir() / "recovery.log",
                    "recovery mode (aggressive)" if aggressive
                    else "recovery mode (standard):\n" + "\n".join(actions))
    return {"actions": actions, "aggressive": aggressive}


# ---------------------------------------------------------------------------
# The supervisor loop
# ---------------------------------------------------------------------------

class Guardian:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self._once_mode = bool(cfg.get("once", False))
        self.state: Dict[str, Any] = {
            "guardian_pid": os.getpid(),
            "version": VERSION,
            "started_at": time.time(),
            "child_pid": None,
            "last_started_at": None,
            "restarts": 0,
            "crashes": 0,
            "boot_failures": 0,
            "hang_kills": 0,
            "recovery_runs": 0,
            "circuit_open": False,
            "last_classification": None,
            "last_exit_code": None,
            "last_stderr_tail": None,
            "last_error": None,
        }
        self._restart_times: Deque[float] = collections.deque()
        self._consecutive_boot_failures = 0
        self._recovery_level = 0  # 0 none, 1 standard, 2 aggressive
        self._backoff = float(cfg.get("backoff_initial_seconds", 1.0))
        self._stderr_tail: Deque[str] = collections.deque(
            maxlen=_STDERR_TAIL_LINES)
        self._stop_requested = threading.Event()
        self._child: Optional[subprocess.Popen] = None
        self._killing_child = False

    # -- child management ---------------------------------------------------

    def _spawn(self) -> Optional[subprocess.Popen]:
        cmd = str(self.cfg.get("command", "jarvis serve"))
        cwd = self.cfg.get("cwd")
        self._stderr_tail.clear()
        self.state["last_started_at"] = time.time()
        self.state["last_classification"] = None
        # A heartbeat left over from the previous run would instantly look
        # like a hang while the new server is still booting — remove it so
        # the fresh process gets a clean liveness window.
        try:
            _heartbeat_path().unlink(missing_ok=True)
        except OSError:
            pass
        try:
            proc = subprocess.Popen(
                cmd,
                shell=True,
                cwd=cwd,
                env=dict(os.environ),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=False,
            )
        except OSError as exc:
            self.state["last_error"] = f"spawn failed: {exc}"
            _append_log(guardian_dir() / "crash-report.log",
                        f"spawn failed: {cmd!r}: {exc}")
            return None
        self._child = proc
        self.state["child_pid"] = proc.pid
        # Drain stdout so the OS pipe buffer never blocks the child.
        threading.Thread(target=self._drain, args=(proc,), daemon=True).start()
        return proc

    def _drain(self, proc: subprocess.Popen) -> None:
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self._stderr_tail.append(line)
        except (OSError, ValueError):
            pass

    def _terminate_child(self, reason: str) -> None:
        proc = self._child
        if proc is None or proc.poll() is not None:
            return
        self._killing_child = True
        _append_log(guardian_dir() / "crash-report.log",
                    f"terminating child (pid={proc.pid}) — {reason}")
        try:
            proc.terminate()
            try:
                proc.wait(timeout=float(
                    self.cfg.get("graceful_termination_seconds", 12.0)))
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        except OSError:
            pass

    # -- classification ---------------------------------------------------

    def _classify_exit(self, proc: subprocess.Popen, uptime: float) -> str:
        if self._killing_child:
            return "guardian_termination"
        intentional = last_heartbeat_says_stopping()
        if proc.returncode in (0, -int(signal.SIGTERM), -int(signal.SIGINT)) \
                and intentional:
            return "clean_stop"
        if uptime < float(self.cfg.get("boot_window_seconds", 120.0)):
            return "boot_failure"
        return "crash"

    def _write_crash_report(self, classification: str,
                            proc: subprocess.Popen, uptime: float) -> None:
        tail = list(self._stderr_tail)[-40:]
        text = (
            f"classification: {classification}\n"
            f"exit code: {proc.returncode}\n"
            f"uptime: {uptime:.1f}s\n"
            f"command: {self.cfg.get('command')}\n"
            f"--- last output ---\n" + "\n".join(tail)
        )
        _append_log(guardian_dir() / "crash-report.log", text)
        self.state["last_stderr_tail"] = "\n".join(tail[-12:])
        self.state["last_exit_code"] = proc.returncode
        self.state["last_classification"] = classification

    # -- control file -------------------------------------------------------

    def _check_ctl(self) -> Optional[str]:
        data = _read_json(_ctl_path())
        if data is None:
            return None
        action = str(data.get("action", "")).lower()
        try:
            _ctl_path().unlink(missing_ok=True)
        except OSError:
            pass
        return action or None

    # -- circuit breaker ----------------------------------------------------

    def _circuit_should_open(self) -> bool:
        limit = int(self.cfg.get("max_restarts_per_hour", 12))
        now = time.time()
        while self._restart_times and now - self._restart_times[0] > 3600.0:
            self._restart_times.popleft()
        return len(self._restart_times) >= limit

    def _open_circuit(self) -> None:
        self.state["circuit_open"] = True
        self.state["circuit_opened_at"] = time.time()
        _append_log(
            guardian_dir() / "circuit.log",
            "circuit breaker OPEN — too many restarts in the last hour.\n"
            f"restarts={len(self._restart_times)} last_error="
            f"{self.state.get('last_classification')} exit="
            f"{self.state.get('last_exit_code')}\n"
            "The service stays down until the hour passes or you run: "
            "python guardian.py --resume\n"
            "Inspect crash-report.log for the root cause.")
        print("[guardian] circuit breaker OPEN — parking service "
              f"({len(self._restart_times)} restarts/hour limit "
              f"{self.cfg.get('max_restarts_per_hour')}). Run --resume to retry.",
              flush=True)

    def _close_circuit(self) -> None:
        self.state["circuit_open"] = False
        self.state.pop("circuit_opened_at", None)
        self._restart_times.clear()

    # -- main loop ----------------------------------------------------------

    def run(self) -> int:
        print(f"[guardian] supervising: {self.cfg.get('command')}", flush=True)
        first_token = str(self.cfg.get("command", "")).split()
        if first_token:
            tok = first_token[0]
            if "/" not in tok and shutil.which(tok) is None:
                print(f"[guardian] warning: {tok!r} not found on PATH — boot "
                      "failures will follow; edit the 'command' field in "
                      f"{_config_path()} to fix.", flush=True)

        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

        while not self._stop_requested.is_set():
            ctl = self._check_ctl()
            if ctl == "resume":
                self._close_circuit()
                print("[guardian] circuit breaker cleared — resuming.",
                      flush=True)
            elif ctl == "stop":
                self._request_stop("ctl stop requested")
                break

            if self._circuit_should_open():
                if not self.state.get("circuit_open"):
                    self._open_circuit()
            if self.state.get("circuit_open"):
                # Park: service down, guardian alive. Short wait slices so
                # ctl resume/stop requests are honored within seconds.
                self.state["child_pid"] = None
                _atomic_write_json(_state_path(), self.state)
                if self._stop_requested.wait(2.0):
                    break
                continue

            proc = self._spawn()
            if proc is None:
                self.state["boot_failures"] += 1
                if self._once_mode:
                    _atomic_write_json(_state_path(), self.state)
                    print("[guardian] --once: spawn failed, exiting.", flush=True)
                    break
                if self._park_or_backoff("spawn_failure"):
                    break
                continue

            outcome = self._supervise_one(proc)
            if outcome == "stop_requested":
                break

            _atomic_write_json(_state_path(), self.state)

            if self._once_mode:
                print("[guardian] --once: supervised run finished, exiting.",
                      flush=True)
                break

        self._final_shutdown()
        return 0

    def _supervise_one(self, proc: subprocess.Popen) -> str:
        """Watch a running child until it exits or action is required.

        Returns 'stop_requested' when the guardian should exit, else the
        classification of the run's end.
        """
        started = time.time()
        self._killing_child = False
        while True:
            ret = proc.poll()
            if ret is not None:
                uptime = time.time() - started
                classification = self._classify_exit(proc, uptime)
                self._register_end(classification, proc, uptime)
                return classification

            # Control file may arrive while the child runs.
            ctl = self._check_ctl()
            if ctl == "stop":
                self._terminate_child("ctl stop")
                self._request_stop("ctl stop requested")
                return "stop_requested"
            if ctl == "resume":
                self._close_circuit()

            # Hang detection: stale heartbeat from the in-server watchdog.
            stale = heartbeat_is_stale(self.cfg)
            if stale is not None and not self._killing_child:
                self.state["hang_kills"] += 1
                self.state["last_classification"] = "hang"
                _append_log(guardian_dir() / "crash-report.log",
                            f"hang detected — heartbeat stale for "
                            f"{stale.get('_age_s')}s (pid={proc.pid}); "
                            "terminating and restarting.")
                print(f"[guardian] HANG detected (heartbeat stale "
                      f"{stale.get('_age_s')}s) — restarting", flush=True)
                self._terminate_child("hang")
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    pass
                self._register_restart()
                return "hang"

            # Fallback liveness probe when no heartbeat exists.
            if _heartbeat_path().exists() is False and port_is_down(self.cfg) \
                    and time.time() - started > 60.0:
                # Only meaningful if a port probe is configured.
                if self.cfg.get("port_probe"):
                    self.state["hang_kills"] += 1
                    print("[guardian] port probe unreachable — restarting",
                          flush=True)
                    self._terminate_child("port_probe")
                    try:
                        proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        pass
                    self._register_restart()
                    return "hang"

            if self._stop_requested.is_set():
                self._terminate_child("guardian shutdown")
                return "stop_requested"

            time.sleep(float(self.cfg.get("check_interval_seconds", 5.0)))

    def _register_end(self, classification: str, proc: subprocess.Popen,
                      uptime: float) -> None:
        self._write_crash_report(classification, proc, uptime)
        print(f"[guardian] child ended: {classification} "
              f"(exit={proc.returncode}, uptime={uptime:.1f}s)", flush=True)

        if classification == "clean_stop":
            if not self.cfg.get("restart_on_clean_exit", False):
                self._request_stop("clean stop (intentional)")
            return

        if classification == "boot_failure":
            self.state["boot_failures"] += 1
            self._consecutive_boot_failures += 1
        elif classification == "crash":
            self.state["crashes"] += 1
            self._consecutive_boot_failures = 0
        elif classification == "guardian_termination":
            return  # guardian itself decided; no new restart bookkeeping

        # Stable run resets the backoff ladder and recovery escalation.
        if uptime > float(self.cfg.get("stable_after_seconds", 300.0)):
            self._consecutive_boot_failures = 0
            self._recovery_level = 0
            self._backoff = float(self.cfg.get("backoff_initial_seconds", 1.0))

        self._park_or_backoff(classification)

    def _register_restart(self) -> None:
        self.state["restarts"] = int(self.state.get("restarts", 0)) + 1
        self._restart_times.append(time.time())

    def _park_or_backoff(self, classification: str) -> bool:
        """Handle boot-failure escalation + backoff sleep.

        Returns True when the guardian should exit entirely.
        """
        threshold = int(self.cfg.get("boot_failures_before_recovery", 3))
        needs_standard = (self._consecutive_boot_failures >= threshold
                          and self._recovery_level < 1)
        needs_aggressive = (self._consecutive_boot_failures >= threshold * 2
                            and self._recovery_level < 2)
        if needs_standard or needs_aggressive:
            aggressive = needs_aggressive and not needs_standard
            print(f"[guardian] {self._consecutive_boot_failures} consecutive "
                  f"boot failures — entering recovery mode"
                  f"{' (aggressive)' if aggressive else ''}", flush=True)
            report = run_recovery_mode(aggressive=aggressive)
            self._recovery_level = 2 if aggressive else 1
            self.state["recovery_runs"] = \
                int(self.state.get("recovery_runs", 0)) + 1
            self.state["last_recovery"] = report
            self._append_log_recovery(report)
            # After recovery, give the fix a fair chance: reset backoff.
            self._backoff = float(self.cfg.get("backoff_initial_seconds", 1.0))

        if classification in ("crash", "boot_failure", "hang",
                              "spawn_failure"):
            self._register_restart()

        if self._once_mode:
            return False  # single run — no backoff sleep, caller exits

        wait = min(self._backoff, float(self.cfg.get("backoff_max_seconds", 60.0)))
        self._backoff = min(self._backoff * 2.0,
                            float(self.cfg.get("backoff_max_seconds", 60.0)))
        if wait > 0:
            print(f"[guardian] restarting in {wait:.0f}s "
                  f"(classification={classification})", flush=True)
            if self._stop_requested.wait(wait):
                return True
        return False

    def _append_log_recovery(self, report: Dict[str, Any]) -> None:
        actions = report.get("actions") or ["nothing needed repair"]
        _append_log(guardian_dir() / "recovery.log",
                    "post-boot-failure recovery:\n" + "\n".join(actions))

    # -- shutdown -----------------------------------------------------------

    def _on_signal(self, signum: int, frame: Any) -> None:
        self._request_stop(f"signal {signum}")

    def _request_stop(self, reason: str) -> None:
        if not self._stop_requested.is_set():
            print(f"[guardian] stopping ({reason})", flush=True)
        self._stop_requested.set()

    def _final_shutdown(self) -> None:
        self._terminate_child("guardian shutdown")
        self.state["child_pid"] = None
        self.state["stopped_at"] = time.time()
        _atomic_write_json(_state_path(), self.state)
        print("[guardian] down.", flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_status() -> int:
    cfg = load_config()
    state = _read_json(_state_path()) or {}
    hb = _read_json(_heartbeat_path())
    print(json.dumps({
        "config": {k: cfg[k] for k in ("command", "max_restarts_per_hour",
                                       "heartbeat_stale_seconds")},
        "state": state or "no guardian run recorded",
        "heartbeat": hb or "none (server not started / watchdog absent)",
        "heartbeat_age_s": (
            round(time.time() - float(hb["ts"]), 1)
            if hb and "ts" in hb else None),
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_stop() -> int:
    _atomic_write_json(_ctl_path(), {"action": "stop",
                                     "requested_at": time.time()})
    print("stop request written — the guardian will terminate the server "
          "and exit. (If the guardian is not running, nothing happens.)")
    return 0


def cmd_resume() -> int:
    _atomic_write_json(_ctl_path(), {"action": "resume",
                                     "requested_at": time.time()})
    print("resume request written — the circuit breaker will clear.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="guardian", description=__doc__.splitlines()[0])
    parser.add_argument("--exec", dest="command",
                        help="command to supervise (default: 'jarvis serve')")
    parser.add_argument("--status", action="store_true",
                        help="print a health snapshot and exit")
    parser.add_argument("--stop", action="store_true",
                        help="ask a running guardian to stop (graceful)")
    parser.add_argument("--resume", action="store_true",
                        help="clear the circuit breaker")
    parser.add_argument("--recover", action="store_true",
                        help="run recovery mode once and exit (no supervising)")
    parser.add_argument("--once", action="store_true",
                        help="supervise a single run; never restart")
    parser.add_argument("--version", action="version",
                        version=f"guardian {VERSION}")
    args = parser.parse_args(argv)

    if args.status:
        return cmd_status()
    if args.stop:
        return cmd_stop()
    if args.resume:
        return cmd_resume()

    cfg = ensure_config()
    if args.command:
        cfg["command"] = args.command
    if args.recover:
        report = run_recovery_mode(aggressive=False)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    if args.once:
        cfg["once"] = True
        # Single diagnostic run: never restart, never escalate recovery.
        cfg["boot_failures_before_recovery"] = 10 ** 6
        cfg["max_restarts_per_hour"] = 10 ** 6

    guardian = Guardian(cfg)
    try:
        return guardian.run()
    except KeyboardInterrupt:
        guardian._request_stop("ctrl-c")
        guardian._final_shutdown()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
