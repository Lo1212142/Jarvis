"""Creative suite — agentic media generation & editing for OpenJarvis.

A fully additive package that gives Jarvis professional creative powers:

* **Generation** — ``media_image_generate`` (NVIDIA NIM first-class,
  provider-flexible) and ``media_video_generate`` (t2v/i2v, async polling).
* **Editing studio** — ``image_edit`` (40+ Pillow/OpenCV operations) and
  ``video_edit`` (single ops + a full multi-track timeline DSL with
  transitions, Ken Burns, effects, text, audio mixing) — the headless,
  programmable OpenCut replacement.
* **Demo composer** — ``demo_video``: AI-company-style launch videos.
* **Knowledge** — ``tech_news`` deep tech/science briefings.
* **Tutoring** — ``tutor``: sessions, quizzes, mastery, spaced repetition.
* **Memory** — ``remember_preference`` + an automatic chat listener that
  captures "سجل عندك…" statements into prompt-visible memory.
* **Self-development** — ``self_dev_build``: Jarvis writes, validates,
  activates and (on failure) rolls back its own new tools; the
  self-healing watcher keeps everything healthy.

Integration is one call: ``install_creative_routes(app)`` plus one import
block in ``tools/__init__.py`` (see the integration guide).
"""

from __future__ import annotations

from openjarvis.creative.creative_routes import install_creative_routes

__all__ = ["install_creative_routes"]
