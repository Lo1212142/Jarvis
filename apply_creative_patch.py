#!/usr/bin/env python3
"""Idempotent surgical patcher — merges the Creative Suite into an
already-modified OpenJarvis checkout WITHOUT overwriting user changes.

Safe by design:
* Only INSERTS small blocks at known anchor points.
* Skips any edit whose marker already exists (re-runnable).
* Never deletes or rewrites existing lines.
* Refuses to touch a file when the anchor is not found (reports instead).

Usage:
    python3 apply_creative_patch.py            # patch repo in CWD
    python3 apply_creative_patch.py /path/to/OpenJarvis-server-source
"""
from __future__ import annotations

import sys
from pathlib import Path

# (file, anchor, insertion, marker, description)
BACKEND_EDITS = [
    (
        "src/openjarvis/tools/__init__.py",
        '__all__ = ["BaseTool", "ToolExecutor", "ToolSpec"]',
        '''# Creative suite (media generation + editing studio + tutor/news/memory +
# self-development + GEOINT forensics + Shodan) — additive package, safe to skip.
try:
    import openjarvis.creative.image_tools  # noqa: F401
    import openjarvis.creative.video_tools  # noqa: F401
    import openjarvis.creative.demo_video_tool  # noqa: F401
    import openjarvis.creative.news_tool  # noqa: F401
    import openjarvis.creative.tutor_tool  # noqa: F401
    import openjarvis.creative.preferences_tool  # noqa: F401
    import openjarvis.creative.self_dev  # noqa: F401
    import openjarvis.creative.geoint_tools  # noqa: F401
    import openjarvis.creative.geoint_map_tools  # noqa: F401
    import openjarvis.creative.geoint_satellite  # noqa: F401
    import openjarvis.creative.shodan_tools  # noqa: F401
except ImportError:
    pass

''',
        "import openjarvis.creative.image_tools",
        "tools/__init__.py: register creative tools",
    ),
    # Upgrade path: installs patched with v1.x already have the block above
    # WITHOUT the GEOINT lines — add just those after the self_dev import.
    (
        "src/openjarvis/tools/__init__.py",
        "    import openjarvis.creative.self_dev  # noqa: F401",
        """    import openjarvis.creative.geoint_tools  # noqa: F401
    import openjarvis.creative.geoint_map_tools  # noqa: F401
    import openjarvis.creative.geoint_satellite  # noqa: F401""",
        "import openjarvis.creative.geoint_tools",
        "tools/__init__.py: register GEOINT tools (v1.2 upgrade)",
    ),
    # Upgrade path: v1.2 installs already have GEOINT — append Shodan after.
    (
        "src/openjarvis/tools/__init__.py",
        "    import openjarvis.creative.geoint_satellite  # noqa: F401",
        "    import openjarvis.creative.shodan_tools  # noqa: F401",
        "import openjarvis.creative.shodan_tools",
        "tools/__init__.py: register Shodan tool (v1.3 upgrade)",
    ),
    (
        "src/openjarvis/server/app.py",
        "    include_all_routes(app)",
        '''
    # Creative suite (media generation + editing studio + tutor/news/
    # preference memory + self-development/self-heal) — additive package.
    try:
        from openjarvis.creative import install_creative_routes

        install_creative_routes(app)
    except Exception as exc:
        logger.warning("Creative suite init skipped: %s", exc)
''',
        "install_creative_routes(app)",
        "server/app.py: mount creative routes + listeners",
    ),
]

FRONTEND_EDITS = [
    (
        "frontend/src/App.tsx",
        "import { LogsPage } from './pages/LogsPage';",
        "import { CreativeStudioPage } from './pages/CreativeStudioPage';\n",
        "CreativeStudioPage",
        "App.tsx: import the studio page",
    ),
    (
        "frontend/src/App.tsx",
        '<Route path="agents" element={<AgentsPage />} />',
        '          <Route path="creative-studio" element={<CreativeStudioPage />} />\n',
        'path="creative-studio"',
        "App.tsx: add the studio route",
    ),
    (
        "frontend/src/components/Sidebar/Sidebar.tsx",
        "  Database,\n} from 'lucide-react';",
        "  Database,\n  Clapperboard,\n} from 'lucide-react';",
        "Clapperboard,",
        "Sidebar.tsx: import icon",
    ),
    (
        "frontend/src/components/Sidebar/Sidebar.tsx",
        "{ path: '/agents', icon: Bot, label: 'Agents' },",
        "    { path: '/creative-studio', icon: Clapperboard, label: 'Creative Studio' },\n",
        "/creative-studio",
        "Sidebar.tsx: add nav entry",
    ),
]


def apply(root: Path) -> int:
    changed, skipped, failed = [], [], []
    for rel_path, anchor, insertion, marker, description in BACKEND_EDITS + FRONTEND_EDITS:
        path = root / rel_path
        if not path.exists():
            failed.append(f"{description} — file missing: {rel_path}")
            continue
        text = path.read_text("utf-8")
        if marker in text:
            skipped.append(description)
            continue
        if anchor not in text:
            failed.append(f"{description} — anchor not found in {rel_path}:\n"
                          f"        {anchor[:80]}")
            continue
        # Frontend route insert goes after the anchor line; backend inserts
        # also after the anchor (newline-normalized).
        replaced = text.replace(anchor, anchor + "\n" + insertion.rstrip("\n") + "\n", 1)
        path.write_text(replaced, "utf-8")
        changed.append(description)
    print("=" * 62)
    for item in changed:
        print(f"  [APPLIED ] {item}")
    for item in skipped:
        print(f"  [SKIP    ] {item} (already patched)")
    for item in failed:
        print(f"  [MANUAL  ] {item}")
    print("=" * 62)
    if failed:
        print(f"\n{len(failed)} edit(s) need manual application — see the "
              "insertion blocks above / INTEGRATION.md.")
        return 1
    print("\nAll surgical edits applied cleanly. Restart `jarvis serve` and "
          "rebuild the frontend (npm run build) to activate.")
    return 0


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    if not (root / "src" / "openjarvis").exists():
        print(f"error: {root} does not look like the OpenJarvis server source "
              "root (src/openjarvis missing)")
        return 2
    return apply(root)


if __name__ == "__main__":
    raise SystemExit(main())
