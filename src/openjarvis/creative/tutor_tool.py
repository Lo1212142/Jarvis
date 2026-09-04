"""Tutor tool — a structured teaching / study system the agent drives.

The tool provides the *machinery* of professional tutoring so Jarvis can
teach like a pro:

* ``start``  — open a study session (topic, level, goals) with a generated
  lesson skeleton the agent fills with real teaching content.
* ``outline`` — request a lesson plan structure for a topic/level.
* ``quiz``   — ask the engine to serve stored questions (MCQ/flashcard),
  grade an answer, record mastery.
* ``add_questions`` — the agent stores generated questions per topic.
* ``progress`` — full learning analytics: mastery per topic, streaks,
  spaced-repetition due list.
* ``due``    — spaced-repetition scheduling (SM-2 style) for review topics.

State persists in ``~/.openjarvis/tutor/`` (sessions, questions, progress)
so learning survives restarts. Everything is local JSON — no network.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()

_LEVELS = {
    "beginner": "Assume zero prior knowledge. Use everyday analogies, short"
                " sentences, and define every term. Build confidence first.",
    "intermediate": "Assume basic familiarity. Connect ideas, introduce"
                    " precise terminology, and include worked examples.",
    "advanced": "Go deep: edge cases, formal definitions, trade-offs, and"
                " mental models experts use.",
}


def _tutor_root() -> Path:
    from openjarvis.core.paths import get_config_dir

    root = get_config_dir() / "tutor"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _load(name: str, default: Any) -> Any:
    path = _tutor_root() / name
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text("utf-8") or "null") or default
    except (json.JSONDecodeError, OSError):
        return default


def _save(name: str, data: Any) -> None:
    path = _tutor_root() / name
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
    tmp.replace(path)


def _quality_to_score(quality: int) -> float:
    """Map SM-2 grade (0-5) to a 0-1 mastery delta."""
    return max(0.0, min(1.0, (quality - 1) / 4.0))


def _next_review(interval_days: float, repetitions: int, quality: int) -> tuple[float, int]:
    """SM-2-lite scheduling: returns (interval_days, repetitions)."""
    if quality < 3:
        return 1.0, 0
    if repetitions == 0:
        return 1.0, repetitions + 1
    if repetitions == 1:
        return 3.0, repetitions + 1
    return interval_days * 2.2, repetitions + 1


_OUTLINE_TEMPLATES = {
    "language": [
        "Warm-up & motivation (why this language matters to you)",
        "Core vocabulary set A (10 items) with pronunciation",
        "Grammar pattern 1 explained with 3 examples",
        "Guided practice: build 5 sentences using set A + pattern 1",
        "Listening micro-drill (agent reads, learner types)",
        "Vocabulary set B (10 items) + pattern 2",
        "Production: free conversation (agent corrects gently)",
        "Review + spaced-repetition plan for today's items",
    ],
    "general": [
        "Hook: one surprising fact or problem that motivates the topic",
        "What you will be able to do after this lesson (goals)",
        "Core concept 1 — explained with an analogy",
        "Check question 1 (agent verifies before moving on)",
        "Core concept 2 — worked example, step by step",
        "Check question 2",
        "Common mistakes and how to avoid them",
        "Practice set (3 graded exercises, easy → hard)",
        "Summary map of the whole lesson",
        "What's next + spaced-repetition schedule",
    ],
}


def build_outline(topic: str, level: str, kind: str = "general") -> List[str]:
    template = _OUTLINE_TEMPLATES.get(kind if kind in _OUTLINE_TEMPLATES else "general")
    level_note = _LEVELS.get(level, _LEVELS["intermediate"])
    outline = [f"Lesson outline: {topic} ({level})"]
    outline.append(f"Teaching style: {level_note}")
    outline.append("")
    for i, step in enumerate(template, 1):
        outline.append(f"{i}. {step}")
    return outline


@ToolRegistry.register("tutor")
class TutorTool(BaseTool):
    """Professional tutor engine — sessions, quizzes, spaced repetition."""

    tool_id = "tutor"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="tutor",
            description=(
                "Run a professional tutoring / study session. The tool"
                " manages structure and memory (sessions, question banks,"
                " mastery tracking, spaced repetition) while the agent"
                " teaches. Actions: start (open session + get lesson"
                " outline), outline (lesson plan for a topic/level),"
                " add_questions (store MCQs/flashcards you generated),"
                " quiz (serve the next question), grade (score an answer,"
                " update mastery + schedule next review), progress"
                " (analytics: mastery per topic, streaks, due list), due"
                " (what the learner should review now). Works for English"
                " learning, school subjects, or any topic — ask the"
                " learner's level, adapt, and always close the loop with"
                " the quiz engine."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["start", "outline", "add_questions", "quiz",
                                 "grade", "progress", "due"],
                        "description": "Tutoring action to perform.",
                    },
                    "topic": {"type": "string", "description": "Subject / topic."},
                    "level": {
                        "type": "string", "enum": ["beginner", "intermediate", "advanced"],
                        "description": "Learner level.",
                    },
                    "kind": {
                        "type": "string", "enum": ["general", "language"],
                        "description": "Lesson template style.",
                    },
                    "session_id": {"type": "string", "description": "Active session id."},
                    "learner": {"type": "string", "description": "Learner name (default 'default')."},
                    "questions": {
                        "type": "array", "items": {"type": "object"},
                        "description": "For add_questions: [{question, choices?, answer, explanation?}].",
                    },
                    "question_id": {"type": "string", "description": "For grade: the served question id."},
                    "answer": {"type": "string", "description": "For grade: learner's answer."},
                    "quality": {
                        "type": "integer", "minimum": 0, "maximum": 5,
                        "description": "For grade: SM-2 grade 0-5 (5=perfect).",
                    },
                },
                "required": ["action"],
            },
            category="education",
            timeout_seconds=30.0,
        )

    # -- actions -------------------------------------------------------------

    @staticmethod
    def _start(params: Dict[str, Any]) -> str:
        topic = str(params.get("topic") or "general").strip()
        level = str(params.get("level") or "intermediate")
        kind = str(params.get("kind") or ("language" if _looks_like_language(topic) else "general"))
        learner = str(params.get("learner") or "default")
        with _LOCK:
            sessions = _load("sessions.json", {})
            session_id = f"s-{uuid.uuid4().hex[:8]}"
            session = {
                "id": session_id,
                "learner": learner,
                "topic": topic,
                "level": level,
                "kind": kind,
                "started_at": time.time(),
                "steps_done": 0,
                "outline": build_outline(topic, level, kind),
                "status": "active",
            }
            sessions[session_id] = session
            _save("sessions.json", sessions)
        questions = _load("questions.json", {}).get(topic, [])
        return (
            f"Session {session_id} started for **{topic}**"
            f" ({level}, {kind}).\n\nLesson plan:\n" + "\n".join(session["outline"]) +
            f"\n\nStored questions for this topic: {len(questions)}."
            "\nTeach step by step — after each concept, quiz the learner,"
            " then call tutor grade to record mastery."
        )

    @staticmethod
    def _add_questions(params: Dict[str, Any]) -> str:
        topic = str(params.get("topic") or "").strip()
        questions = list(params.get("questions") or [])
        if not topic or not questions:
            return "'topic' and 'questions' are required."
        clean = []
        for q in questions:
            if not isinstance(q, dict) or not q.get("question"):
                continue
            clean.append({
                "id": f"q-{uuid.uuid4().hex[:8]}",
                "question": str(q.get("question")),
                "choices": [str(c) for c in (q.get("choices") or [])],
                "answer": str(q.get("answer") or ""),
                "explanation": str(q.get("explanation") or ""),
            })
        if not clean:
            return "No valid questions provided (need 'question' fields)."
        with _LOCK:
            bank = _load("questions.json", {})
            topic_bank = bank.get(topic) or []
            topic_bank.extend(clean)
            bank[topic] = topic_bank
            _save("questions.json", bank)
        return f"Added {len(clean)} questions to '{topic}' (bank now {len(topic_bank)})."

    @staticmethod
    def _quiz(params: Dict[str, Any]) -> str:
        topic = str(params.get("topic") or "").strip()
        bank = _load("questions.json", {}).get(topic) or []
        if not bank:
            return (f"No stored questions for '{topic}'. Generate 5-10 MCQs"
                    " now (with answers + explanations), then call"
                    " add_questions so grading and scheduling work.")
        progress = _load("progress.json", {})
        learner = str(params.get("learner") or "default")
        learner_progress = progress.get(learner) or {}
        now = time.time()
        due = [q for q in bank
               if (learner_progress.get(_progress_key(topic, q["id"]), {})
                   .get("next_review", 0)) <= now]
        pool = due or bank
        question = pool[0]
        choices = ""
        if question.get("choices"):
            choices = "\n" + "\n".join(
                f"  {'ABCD'[i] if i < 4 else i}. {c}"
                for i, c in enumerate(question["choices"])
            )
        return (
            f"QUIZ [{question['id']}] topic='{topic}':\n"
            f"{question['question']}{choices}\n"
            f"(serve this to the learner; when they answer, call grade"
            f" with question_id='{question['id']}', their answer, and"
            f" quality 0-5)"
        )

    @staticmethod
    def _grade(params: Dict[str, Any]) -> str:
        topic = str(params.get("topic") or "").strip()
        question_id = str(params.get("question_id") or "")
        answer = str(params.get("answer") or "").strip()
        quality = int(params.get("quality") or -1)
        learner = str(params.get("learner") or "default")
        bank = _load("questions.json", {}).get(topic) or []
        question = next((q for q in bank if q["id"] == question_id), None)
        if question is None:
            return f"Question '{question_id}' not found for topic '{topic}'."
        correct = answer.strip().lower() == str(question.get("answer", "")).strip().lower()
        if quality < 0:
            quality = 5 if correct else 2
        with _LOCK:
            progress = _load("progress.json", {})
            lp = progress.get(learner) or {}
            key = _progress_key(topic, question_id)
            entry = lp.get(key) or {"repetitions": 0, "interval": 1.0,
                                    "mastery": 0.0}
            interval, reps = _next_review(entry["interval"],
                                          entry["repetitions"], quality)
            entry.update({
                "repetitions": reps,
                "interval": round(interval, 2),
                "mastery": round(min(1.0, entry["mastery"] + (0.15 if correct else -0.1)), 3),
                "last_quality": quality,
                "next_review": time.time() + interval * 86400,
                "last_seen": time.time(),
            })
            lp[key] = entry
            streak = lp.get("__streak__", {})
            today = time.strftime("%Y-%m-%d")
            if correct:
                if streak.get("date") == today:
                    streak["count"] = int(streak.get("count", 0)) + 1
                else:
                    streak.update({"date": today, "count": 1})
            lp["__streak__"] = streak
            progress[learner] = lp
            _save("progress.json", progress)
        return (
            f"Graded {question_id}: {'✓ correct' if correct else '✗ incorrect'}"
            f" (quality {quality}). Mastery now {entry['mastery']}, next"
            f" review in {interval:.1f} days."
            + (f"\nExplanation to give: {question.get('explanation')}"
               if question.get("explanation") else "")
        )

    @staticmethod
    def _progress(params: Dict[str, Any]) -> str:
        learner = str(params.get("learner") or "default")
        progress = _load("progress.json", {}).get(learner) or {}
        streak = progress.get("__streak__", {})
        topics: Dict[str, Dict[str, float]] = {}
        for key, entry in progress.items():
            if key.startswith("__") or not isinstance(entry, dict):
                continue
            topic, _qid = key.rsplit("|", 1)
            agg = topics.setdefault(topic, {"mastery_sum": 0.0, "n": 0})
            agg["mastery_sum"] += float(entry.get("mastery", 0))
            agg["n"] += 1
        if not topics:
            return (f"No progress recorded yet for '{learner}'. Start a"
                    " session and grade a few answers first.")
        lines = [f"Learning progress for **{learner}**",
                 f"Current streak: {streak.get('count', 0)} correct answers"
                 f" ({streak.get('date', '—')})\n"]
        for topic, agg in sorted(topics.items()):
            mastery = agg["mastery_sum"] / agg["n"]
            bar = "█" * int(mastery * 10) + "░" * (10 - int(mastery * 10))
            lines.append(f"- {topic}: [{bar}] {mastery * 100:.0f}%"
                         f" over {agg['n']} questions")
        return "\n".join(lines)

    @staticmethod
    def _due(params: Dict[str, Any]) -> str:
        learner = str(params.get("learner") or "default")
        progress = _load("progress.json", {}).get(learner) or {}
        now = time.time()
        due_by_topic: Dict[str, int] = {}
        for key, entry in progress.items():
            if key.startswith("__") or not isinstance(entry, dict):
                continue
            if float(entry.get("next_review", 0)) <= now:
                topic, _ = key.rsplit("|", 1)
                due_by_topic[topic] = due_by_topic.get(topic, 0) + 1
        if not due_by_topic:
            return "Nothing due for review right now — start a new topic or add questions."
        lines = ["Due for review now (spaced repetition):"]
        for topic, count in sorted(due_by_topic.items()):
            lines.append(f"- {topic}: {count} question(s)")
        lines.append("Serve them via tutor quiz per topic.")
        return "\n".join(lines)

    def execute(self, **params: Any) -> ToolResult:
        action = str(params.get("action") or "").strip().lower()
        handlers = {
            "start": self._start, "outline": lambda p: "\n".join(
                build_outline(str(p.get("topic") or "topic"),
                              str(p.get("level") or "intermediate"),
                              str(p.get("kind") or "general"))
            ),
            "add_questions": self._add_questions, "quiz": self._quiz,
            "grade": self._grade, "progress": self._progress, "due": self._due,
        }
        handler = handlers.get(action)
        if handler is None:
            return ToolResult(tool_name="tutor",
                              content=f"Unknown action '{action}'. Valid: "
                                      f"{', '.join(handlers)}", success=False)
        try:
            content = handler(params)
            ok = not content.startswith("'")  # handlers return errors quoted
            return ToolResult(tool_name="tutor", content=content, success=True)
        except (ValueError, KeyError, OSError) as exc:
            logger.warning("tutor failed: %s", exc)
            return ToolResult(tool_name="tutor",
                              content=f"Tutor action failed: {exc}",
                              success=False)


def _progress_key(topic: str, question_id: str) -> str:
    return f"{topic}|{question_id}"


def _looks_like_language(topic: str) -> bool:
    low = (topic or "").lower()
    return any(word in low for word in ("english", "انجليزي", "إنجليزي",
                                        "arabic", "عربي", "french", "spanish",
                                        "german", "language", "لغة"))


__all__ = ["TutorTool", "build_outline"]
