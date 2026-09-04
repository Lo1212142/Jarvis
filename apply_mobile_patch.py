#!/usr/bin/env python3
"""Apply the OpenJarvis Mobile Companion Suite to a server source tree.

Idempotent: safe to run any number of times. Every edit is anchored and
verified; already-applied edits are detected and skipped.

Usage:
    python apply_mobile_patch.py [SERVER_ROOT] [--dry-run]

SERVER_ROOT defaults to the directory containing this script's parent-of-src
candidates (current dir, or OpenJarvis-server-source-* nearby).

What it does:
  1. Copies src/openjarvis/mobile/** into <server>/src/openjarvis/mobile/
  2. tools/__init__.py   — adds the mobile tools import block (guarded try)
  3. server/app.py       — mounts install_mobile_routes(app) after creative
  4. server/auth_middleware.py — maps /api/mobile to the "events" device scope
  5. Verifies everything imports and the three tools are registered.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG_SRC = HERE / "src"

MARKER = "# mobile companion (server→phone push)"

TOOLS_ANCHOR = "except ImportError:\n    pass\n\n__all__ = [\"BaseTool\", \"ToolExecutor\", \"ToolSpec\"]"
TOOLS_BLOCK = """except ImportError:
    pass

# Mobile companion (server→phone push: notify_user / alert_user /
# mobile_devices_status + proactive watcher) — additive, safe to skip.
try:
    import openjarvis.mobile.notify_tools  # noqa: F401
except ImportError:
    pass

__all__ = ["BaseTool", "ToolExecutor", "ToolSpec"]"""

APP_ANCHOR = """    except Exception as exc:
        logger.warning("Creative suite init skipped: %s", exc)
"""
APP_BLOCK = """    except Exception as exc:
        logger.warning("Creative suite init skipped: %s", exc)

    # Mobile companion — the outbound "Jarvis contacts you" channel
    # (Expo push + agent tools + proactive event watcher). Additive.
    try:
        from openjarvis.mobile import install_mobile_routes

        install_mobile_routes(app)
    except Exception as exc:
        logger.warning("Mobile companion init skipped: %s", exc)
"""

AUTH_ANCHOR = """    if path.startswith("/api/events") or path.startswith("/api/ws"):
        return "events"
"""
AUTH_BLOCK = """    if path.startswith("/api/events") or path.startswith("/api/ws"):
        return "events"
    if path.startswith("/api/mobile"):
        # Server→phone push surface: register tokens, quiet hours, test push.
        return "events"
"""


def find_server_root(explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit).resolve()
        return path if (path / "src" / "openjarvis" / "server").is_dir() else None
    candidates = [HERE, HERE.parent, Path.cwd()]
    for base in candidates:
        if (base / "src" / "openjarvis" / "server").is_dir():
            return base
        for sibling in sorted(base.glob("OpenJarvis-server-source-*")):
            if (sibling / "src" / "openjarvis" / "server").is_dir():
                return sibling
    return None


def patch_file(path: Path, anchor: str, block: str, label: str, dry: bool) -> str:
    text = path.read_text(encoding="utf-8")
    if MARKER in text or block.strip() in text:
        return f"SKIP (already applied): {label}"
    if anchor not in text:
        return f"FAIL (anchor not found): {label}"
    if dry:
        return f"DRY   (would patch): {label}"
    path.write_text(text.replace(anchor, block, 1), encoding="utf-8")
    return f"OK    (patched): {label}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("server_root", nargs="?", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = find_server_root(args.server_root)
    if root is None:
        print("Could not locate the OpenJarvis server source "
              "(pass SERVER_ROOT as the first argument).")
        return 2
    print(f"server root : {root}")

    results = []

    # 1) copy package files
    target = root / "src" / "openjarvis" / "mobile"
    if args.dry_run:
        results.append(f"DRY   (would copy): {PKG_SRC}/openjarvis/mobile -> {target}")
    else:
        target.mkdir(parents=True, exist_ok=True)
        for source in (PKG_SRC / "openjarvis" / "mobile").glob("*.py"):
            shutil.copy2(source, target / source.name)
        for cache in target.glob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)
        copied = sorted(p.name for p in target.glob("*.py"))
        results.append(f"OK    (copied {len(copied)} files): {', '.join(copied)}")

    # 2-4) surgical edits
    results.append(patch_file(root / "src" / "openjarvis" / "tools" / "__init__.py",
                              TOOLS_ANCHOR, TOOLS_BLOCK, "tools/__init__.py import block", args.dry_run))
    results.append(patch_file(root / "src" / "openjarvis" / "server" / "app.py",
                              APP_ANCHOR, APP_BLOCK, "server/app.py install call", args.dry_run))
    results.append(patch_file(root / "src" / "openjarvis" / "server" / "auth_middleware.py",
                              AUTH_ANCHOR, AUTH_BLOCK, "server/auth_middleware.py scope map", args.dry_run))

    for line in results:
        print("  " + line)
    if any(line.startswith("FAIL") for line in results):
        print("\nRESULT: FAILED — inspect the FAIL lines above.")
        return 1

    if args.dry_run:
        print("\nRESULT: DRY-RUN OK (no changes written)")
        return 0

    # 5) import verification against the patched tree (isolated HOME)
    import os

    stage = Path(tempfile.mkdtemp(prefix="oj-apply-verify-"))
    os.environ["OPENJARVIS_HOME"] = str(stage)
    sys.path.insert(0, str((root / "src").resolve()))
    try:
        import openjarvis.mobile.notify_tools  # noqa: F401
        from openjarvis.core.registry import ToolRegistry

        missing = [name for name in ("notify_user", "alert_user", "mobile_devices_status")
                   if not ToolRegistry.contains(name)]
        if missing:
            print(f"\nRESULT: FAILED — tools not registered: {missing}")
            return 1
        from openjarvis.mobile import install_mobile_routes  # noqa: F401

        print("  OK    (verified): all 3 tools registered + package imports cleanly")
        print("\nRESULT: APPLIED SUCCESSFULLY")
        print("Next: restart the service (or POST /api/mobile/hotload on the live server),")
        print("then open the paired Jarivs app once so it registers its push token.")
        return 0
    except Exception as exc:
        print(f"\nRESULT: FAILED — import check: {exc}")
        return 1
    finally:
        sys.path.remove(str((root / "src").resolve()))


if __name__ == "__main__":
    raise SystemExit(main())
