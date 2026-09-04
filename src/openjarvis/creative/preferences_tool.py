"""Instant preference capture — "سجل عندك إني بحب التقنيات" → memory.

Two capture paths, both landing in persistent, prompt-visible memory:

1. **Explicit tool** — ``remember_preference``: the agent (or the user via
   chat) stores a preference with category and confidence. It is written
   to ``MEMORY.md`` (which SystemPromptBuilder injects into every prompt)
   plus a structured index at ``~/.openjarvis/preferences.json``.

2. **Automatic listener** — :func:`install_preference_listener` subscribes
   to ``CHAT_EXCHANGE_COMPLETED`` on the event bus and detects
   preference-statements in the user's message (Arabic + English
   patterns like "سجل عندك…", "remember that I…", "I prefer…"). Detected
   preferences are stored immediately — no tool call needed — exactly the
   "لو قال سجلها فورًا يسجلها" behavior.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()

_MEMORY_HEADING = "## Preferences"

# Preference-category keyword maps for auto-classification.
_CATEGORY_HINTS = {
    "likes": ("بحب", "أحب", "بحببت", "عاجبني", "I like", "I love", "i prefer",
              "favorite", "favourite", "best"),
    "dislikes": ("مش بحب", "لا أحب", "كرهت", "زعلان من", "I hate", "I dislike",
                 "annoying", "I don't like"),
    "habits": ("دايمًا", "دائما", "عادي إني", "my habit", "I always", "I usually"),
    "work": ("شغلي", "شغله", "شغل", "my job", "my work", "at work"),
    "tech": ("تقنية", "تكنولوجيا", "الكمبيوتر", "اللابتوب", "tech", "technology",
             "software", "hardware", "AI", "الذكاء الاصطناعي"),
    "food": ("آكل", "أكل", "الاكل", "food", "eat", "coffee", "قهوة", "شاي"),
    "language": ("عربي", "إنجليزي", "language", "Arabic", "English"),
}

# Patterns that indicate the user is stating a durable preference.
_TRIGGERS = [
    re.compile(r"(?:سجّ?ل\s+(?:عندك|انك|إنك)|احفظ\s+(?:انك|إنك))\s*(?:إني|إني|اني)\s*(.+)", re.IGNORECASE),
    re.compile(r"سجّ?ل\s+عندك\s+(?:إن|اني|إني)\s*(.+)", re.IGNORECASE),
    re.compile(r"(?:تذكر|افتكر)\s*(?:إنك\s*)?(?:إني|اني|إني)\s*(.+)", re.IGNORECASE),
    re.compile(r"remember\s+(?:that\s+)?(?:I|i)\s+(.+)", re.IGNORECASE),
    re.compile(r"(?:keep in mind|note)\s+(?:that\s+)?(?:I|i)\s+(.+)", re.IGNORECASE),
    re.compile(r"(?:I|i)\s+(?:prefer|like|love|hate|always)\s+(.{8,})", re.IGNORECASE),
    re.compile(r"(?:I|i)\s+(?:usually|normally)\s+(.{8,})", re.IGNORECASE),
]

_STOPWORDS = {"من", "في", "على", "عن", "إني", "اني", "إن", "that", "this",
              "these", "those", "please", "لو سمحت", "بسرعة"}


def _memory_path() -> Path:
    from openjarvis.core.paths import get_config_dir

    return get_config_dir() / "MEMORY.md"


def _prefs_path() -> Path:
    from openjarvis.core.paths import get_config_dir

    path = get_config_dir() / "preferences.json"
    return path


def _classify(text: str) -> str:
    low = f" {text.lower()} "
    best, best_hits = "general", 0
    for category, keywords in _CATEGORY_HINTS.items():
        hits = sum(1 for kw in keywords if kw.lower() in low)
        if hits > best_hits:
            best, best_hits = category, hits
    return best


def _clean_statement(statement: str) -> str:
    statement = statement.strip().rstrip(".!؟?،,")
    statement = re.sub(r"\s+", " ", statement)
    # Trim overly long tails (e.g. "… because yesterday when I …").
    if len(statement) > 160:
        statement = statement[:160].rsplit(" ", 1)[0] + "…"
    for word in _STOPWORDS:
        if statement.lower().startswith(word.lower() + " "):
            statement = statement[len(word) + 1:]
    return statement.strip()


def store_preference(
    statement: str,
    *,
    category: Optional[str] = None,
    confidence: float = 0.9,
    source: str = "chat",
) -> Dict[str, Any]:
    """Persist a preference — MEMORY.md (prompt-visible) + JSON index."""
    statement = _clean_statement(statement)
    if len(statement) < 3:
        return {"stored": False, "reason": "statement too short"}
    category = category or _classify(statement)

    # --- JSON index -----------------------------------------------------
    with _LOCK:
        prefs = []
        if _prefs_path().exists():
            try:
                prefs = json.loads(_prefs_path().read_text("utf-8") or "[]")
            except (json.JSONDecodeError, OSError):
                prefs = []
        if not isinstance(prefs, list):
            prefs = []
        # De-duplicate by normalized text.
        norm = re.sub(r"\s+", " ", statement.lower()).strip()
        existing = next((p for p in prefs
                         if re.sub(r"\s+", " ", str(p.get("text", "")).lower()).strip() == norm),
                        None)
        if existing:
            existing["times_seen"] = int(existing.get("times_seen", 1)) + 1
            existing["last_seen"] = time.time()
            entry = existing
        else:
            entry = {
                "id": f"pref-{int(time.time() * 1000) % 10**9:09d}",
                "text": statement,
                "category": category,
                "confidence": round(confidence, 2),
                "source": source,
                "times_seen": 1,
                "created_at": time.time(),
                "last_seen": time.time(),
            }
            prefs.append(entry)
        _prefs_path().write_text(
            json.dumps(prefs, indent=2, ensure_ascii=False), "utf-8"
        )

    # --- MEMORY.md (injected into prompts by SystemPromptBuilder) -------
    try:
        memory = _memory_path()
        content = memory.read_text("utf-8") if memory.exists() else ""
        if statement not in content:
            block = f"- [{category}] {statement} (confidence {confidence:.2f})\n"
            if _MEMORY_HEADING in content:
                content = content.rstrip() + "\n" + block
            else:
                content = content.rstrip() + f"\n\n{_MEMORY_HEADING}\n{block}"
            memory.write_text(content, "utf-8")
    except OSError as exc:
        logger.warning("MEMORY.md update failed: %s", exc)

    return {"stored": True, "entry": entry}


def list_preferences() -> List[Dict[str, Any]]:
    if not _prefs_path().exists():
        return []
    try:
        prefs = json.loads(_prefs_path().read_text("utf-8") or "[]")
        return [p for p in prefs if isinstance(p, dict)]
    except (json.JSONDecodeError, OSError):
        return []


def detect_preference(user_text: str) -> Optional[Dict[str, Any]]:
    """Detect a preference statement in a chat message (AR + EN)."""
    if not user_text:
        return None
    for pattern in _TRIGGERS:
        match = pattern.search(user_text)
        if match and match.group(1):
            statement = _clean_statement(match.group(1))
            if len(statement) >= 4:
                return {"statement": statement,
                        "category": _classify(statement),
                        "trigger": pattern.pattern[:40]}
    return None


def install_preference_listener(bus: Optional[Any]) -> Optional[str]:
    """Subscribe the auto-capture listener to the chat event bus."""
    if bus is None:
        return None

    def _on_exchange(event_type: Any, payload: Dict[str, Any]) -> None:
        try:
            user_text = str(payload.get("user_text") or "")
            if not user_text:
                return
            detected = detect_preference(user_text)
            if detected:
                result = store_preference(
                    detected["statement"],
                    category=detected["category"],
                    confidence=0.85,
                    source="auto-chat-listener",
                )
                if result.get("stored"):
                    logger.info(
                        "preference auto-captured: %s", detected["statement"][:80]
                    )
        except Exception:  # never break the chat flow
            logger.debug("preference listener error", exc_info=True)

    from openjarvis.core.events import EventType

    bus.subscribe(EventType.CHAT_EXCHANGE_COMPLETED, _on_exchange)
    return "installed"


@ToolRegistry.register("remember_preference")
class RememberPreferenceTool(BaseTool):
    """Store a durable user preference into long-term memory, instantly."""

    tool_id = "remember_preference"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="remember_preference",
            description=(
                "Store a durable fact or preference about the user in"
                " long-term memory — effective immediately in the next"
                " prompt (written to MEMORY.md). Use whenever the user"
                " says things like 'سجل عندك إني…', 'remember that I…',"
                "'I prefer…'. Categories: likes, dislikes, habits, work,"
                " tech, food, language, general. Also supports 'list' to"
                " recall stored preferences."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string", "enum": ["store", "list"],
                        "default": "store",
                    },
                    "statement": {
                        "type": "string",
                        "description": "The preference in one clear sentence (e.g. 'I love deep-tech news').",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["likes", "dislikes", "habits", "work", "tech",
                                 "food", "language", "general"],
                        "description": "Preference category (auto-detected if omitted).",
                    },
                    "confidence": {"type": "number", "default": 0.9,
                                   "description": "0-1 confidence."},
                },
                "required": [],
            },
            category="memory",
            timeout_seconds=15.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        action = str(params.get("action") or "store").lower()
        if action == "list":
            prefs = list_preferences()
            if not prefs:
                return ToolResult(tool_name="remember_preference",
                                  content="No stored preferences yet.", success=True)
            lines = [f"{len(prefs)} stored preferences:"]
            for p in prefs:
                age_days = (time.time() - float(p.get("created_at", 0))) / 86400
                lines.append(f"- [{p.get('category')}] {p.get('text')}"
                             f" (seen {p.get('times_seen', 1)}x,"
                             f" ~{age_days:.0f}d ago)")
            return ToolResult(tool_name="remember_preference",
                              content="\n".join(lines), success=True)
        statement = str(params.get("statement") or "").strip()
        if not statement:
            return ToolResult(tool_name="remember_preference",
                              content="No statement provided.", success=False)
        result = store_preference(
            statement,
            category=params.get("category"),
            confidence=float(params.get("confidence") or 0.9),
            source="tool:remember_preference",
        )
        if not result.get("stored"):
            return ToolResult(tool_name="remember_preference",
                              content=result.get("reason", "not stored"),
                              success=False)
        entry = result["entry"]
        content = (
            f"✓ Saved to long-term memory: \"{entry['text']}\"\n"
            f"Category: {entry['category']} | confidence {entry['confidence']}"
            " — I will remember this in every future conversation."
        )
        return ToolResult(tool_name="remember_preference", content=content,
                          success=True)


__all__ = [
    "RememberPreferenceTool",
    "store_preference",
    "list_preferences",
    "detect_preference",
    "install_preference_listener",
]
