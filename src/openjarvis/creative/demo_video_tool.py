"""Demo-video composer — "big AI company" launch-style videos from chat.

One tool call turns a topic + a few scene bullets into a polished product
demo: dark gradient canvas, bold typography (Arabic/English), Ken Burns
motion on imagery, smooth crossfade transitions, watermark, subtle ambient
music bed, and an outro call-to-action — rendered entirely by the local
ffmpeg timeline engine.

If a media provider is configured (NVIDIA NIM by default) and
``generate_media`` is true, the composer first generates b-roll stills for
each scene through text-to-image, then animates them. No browser, no GUI —
the agent composes everything programmatically.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

from openjarvis.creative import _paths, text_render
from openjarvis.creative.ffmpeg_engine import FFMPEG_BIN, FFmpegError, render_timeline

logger = logging.getLogger(__name__)

_ASPECTS = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
}


def _synth_ambient_bed(duration: float, *, workdir: Path) -> Optional[Path]:
    """Generate a subtle ambient music bed (no external assets needed)."""
    out = workdir / "ambient.m4a"
    seconds = max(4, min(duration, 600))
    expression = (
        "0.05*sin(2*PI*196*t)"
        "+0.04*sin(2*PI*294*t)"
        "+0.035*sin(2*PI*392*t)*(0.55+0.45*sin(2*PI*0.08*t))"
        "+0.02*sin(2*PI*494*t)*(0.5+0.5*sin(2*PI*0.05*t+1.5))"
    )
    cmd = [
        FFMPEG_BIN, "-hide_banner", "-nostdin", "-y",
        "-f", "lavfi", "-t", f"{seconds + 2:.2f}",
        "-i", f"aevalsrc={expression}:s=44100",
        "-af", "lowpass=f=2400,afade=t=in:st=0:d=2",
        "-c:a", "aac", "-b:a", "128k", str(out),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if proc.returncode == 0 and out.exists():
            return out
        logger.debug("ambient synth failed: %s", proc.stderr[-300:])
    except Exception as exc:  # pragma: no cover
        logger.debug("ambient synth error: %s", exc)
    return None


def _auto_scenes(topic: str, count: int = 3) -> List[Dict[str, Any]]:
    """Sensible default scene skeleton for a product/tech demo."""
    count = max(1, min(count, 6))
    skeleton = [
        ("Meet {topic}", "A new way to work with {topic} — right from chat."),
        ("Built for speed", "Generate, edit and compose in seconds, not hours."),
        ("Everything included", "Transitions, captions, effects and music — automatic."),
        ("Made for creators", "From idea to finished video in one conversation."),
        ("Always improving", "Every render teaches the studio to do better."),
    ]
    scenes = []
    for i in range(count):
        headline, body = skeleton[i % len(skeleton)]
        scenes.append({
            "headline": headline.format(topic=topic),
            "body": body.format(topic=topic),
        })
    return scenes


@ToolRegistry.register("demo_video")
class DemoVideoTool(BaseTool):
    """Compose AI-company-style demo videos from a topic and scene beats."""

    tool_id = "demo_video"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="demo_video",
            description=(
                "Compose a polished demo/launch video (OpenAI/Google-style"
                " aesthetic) from a topic and optional scene beats. Dark"
                " gradient canvas, bold typography with Arabic support,"
                " Ken Burns motion, smooth transitions, watermark, ambient"
                " music bed and outro CTA — all rendered locally. Scenes:"
                " pass 'scenes' as [{headline, body, media}] or let the"
                " tool structure the topic automatically. Set"
                " generate_media=true to first text-to-image b-roll stills"
                " for every scene via the configured image provider."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "What the demo is about (e.g. 'Jarvis Creative Studio').",
                    },
                    "title": {"type": "string", "description": "Opening title (default: topic)."},
                    "subtitle": {
                        "type": "string",
                        "description": "Opening tagline (e.g. 'Generate • Edit • Compose').",
                    },
                    "scenes": {
                        "type": "array", "items": {"type": "object"},
                        "description": "Scene beats: [{headline, body, media?, kicker?}].",
                    },
                    "style": {
                        "type": "string", "enum": ["dark", "darker", "ocean", "warm", "light"],
                        "default": "dark",
                    },
                    "aspect": {
                        "type": "string", "enum": ["16:9", "9:16", "1:1", "4:5"],
                        "default": "16:9",
                    },
                    "accent": {"type": "string", "default": "#6C8CFF",
                               "description": "Accent color (hex)."},
                    "logo": {"type": "string", "description": "Watermark logo path (optional)."},
                    "music": {
                        "type": "string",
                        "description": "Music file path, 'auto' (synth ambient), or empty to skip.",
                    },
                    "generate_media": {
                        "type": "boolean", "default": False,
                        "description": "Text-to-image b-roll per scene via the configured provider.",
                    },
                    "scene_duration": {
                        "type": "number", "default": 4.0,
                        "description": "Seconds per scene (3-10).",
                    },
                    "cta": {
                        "type": "string",
                        "description": "Outro call-to-action line (e.g. 'Try it today').",
                    },
                    "language": {
                        "type": "string", "enum": ["auto", "en", "ar"],
                        "description": "Hint for auto scene copy (default auto = English).",
                    },
                },
                "required": ["topic"],
            },
            category="media",
            timeout_seconds=1800.0,
        )

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _generate_broll(prompt: str) -> Optional[str]:
        try:
            from openjarvis.creative.providers import generate_image

            result = generate_image(
                prompt, width=1280, height=720, stem="broll"
            )
            return result["images"][0]["path"]
        except Exception as exc:
            logger.info("b-roll generation skipped (%s)", exc)
            return None

    def execute(self, **params: Any) -> ToolResult:
        topic = str(params.get("topic") or "").strip()
        if not topic:
            return ToolResult(tool_name="demo_video",
                              content="No topic provided.", success=False)
        language = str(params.get("language") or "auto")
        arabic = language == "ar" or (
            language == "auto" and text_render.has_arabic(topic)
        )

        width, height = _ASPECTS.get(str(params.get("aspect") or "16:9"), (1920, 1080))
        style = str(params.get("style") or "dark")
        accent = str(params.get("accent") or "#6C8CFF")
        scene_duration = max(2.5, min(10.0, float(params.get("scene_duration") or 4.0)))
        title = str(params.get("title") or topic)
        subtitle = str(params.get("subtitle") or ("توليد • تعديل • احتراف" if arabic else "Generate • Edit • Compose"))
        cta = str(params.get("cta") or ("جرّبه الآن من الشات" if arabic else "Try it now — straight from chat"))
        logo = params.get("logo")
        music = params.get("music")

        raw_scenes = list(params.get("scenes") or [])
        scenes: List[Dict[str, Any]] = []
        for scene in raw_scenes:
            if isinstance(scene, str):
                scenes.append({"headline": scene})
            elif isinstance(scene, dict):
                scenes.append(scene)
        if not scenes:
            scenes = _auto_scenes(topic, count=3)
            if arabic:
                scenes = [
                    {"headline": f"تعرّف على {topic}", "body": "طريقة جديدة للشغل — مباشرة من الشات."},
                    {"headline": "مصمّم للسرعة", "body": "ولّد وعدّل وركّب في ثوانٍ."},
                    {"headline": "كل حاجة جاهزة", "body": "انتقالات وكابشنز ومؤثرات وموسيقى — تلقائي."},
                ]

        workdir = _paths.tmp_workdir()
        try:
            # --- B-roll generation (optional) --------------------------------
            if params.get("generate_media"):
                for scene in scenes:
                    if scene.get("media"):
                        continue
                    prompt = (
                        f"Cinematic b-roll for a tech demo about {topic}:"
                        f" {scene.get('headline', '')}."
                        f" {scene.get('body', '')} Dark premium mood, soft"
                        " volumetric light, 16:9."
                    )
                    media = self._generate_broll(prompt)
                    if media:
                        scene["media"] = media

            # --- Music ----------------------------------------------------------
            music_path: Optional[str] = None
            music_volume = 0.22
            if isinstance(music, str) and music.strip():
                if music.strip().lower() == "auto":
                    bed = _synth_ambient_bed(
                        scene_duration * (len(scenes) + 2) + 4, workdir=workdir
                    )
                    music_path = str(bed) if bed else None
                else:
                    candidate = Path(music.strip()).expanduser()
                    if not candidate.is_absolute():
                        for base in (Path.cwd(), _paths.creative_root()):
                            local = base / candidate
                            if local.exists():
                                candidate = local
                                break
                    if candidate.exists():
                        music_path = str(candidate)
                    else:
                        logger.info("demo music not found, skipping: %s", music)

            # --- Timeline -------------------------------------------------------
            video_clips: List[Dict[str, Any]] = []
            text_clips: List[Dict[str, Any]] = []
            overlay_clips: List[Dict[str, Any]] = []
            audio_clips: List[Dict[str, Any]] = []
            transitions = ["fade", "smoothleft", "circleopen", "dissolve",
                           "smoothright", "slideup", "radial", "fadeblack"]

            def _add_transition(clip: Dict[str, Any], index: int) -> None:
                clip["transition_out"] = {
                    "type": transitions[index % len(transitions)],
                    "duration": 0.7,
                }

            # Title card.
            title_card = workdir / "card_title.png"
            text_render.save_png(
                text_render.render_card(
                    title, canvas_size=(width, height), style=style,
                    subtitle=subtitle, kicker=("عرض توضيحي" if arabic else "DEMO"),
                    accent=accent,
                ),
                title_card,
            )
            title_clip = {
                "src": str(title_card), "kind": "image", "duration": 2.8,
                "kenburns": {"zoom_from": 1.0, "zoom_to": 1.12, "direction": "center"},
                "fade_in": 0.6, "fade_out": 0.4,
            }
            _add_transition(title_clip, 0)
            video_clips.append(title_clip)

            # Scene clips.
            for i, scene in enumerate(scenes):
                headline = str(scene.get("headline") or scene.get("title") or "")
                body = str(scene.get("body") or scene.get("subtitle") or "")
                kicker = str(scene.get("kicker") or (f"0{i + 1}" if not arabic else f"مشهد 0{i + 1}"))
                media = scene.get("media") or scene.get("src")

                clip: Dict[str, Any]
                if media:
                    clip = {
                        "src": str(media), "duration": scene_duration,
                        "fit": "cover",
                        "kenburns": {"zoom_from": 1.0,
                                     "zoom_to": 1.18,
                                     "direction": ("right" if i % 2 else "left")},
                        "fade_out": 0.3,
                    }
                else:
                    card = workdir / f"card_scene_{i:02d}.png"
                    text_render.save_png(
                        text_render.render_gradient_background(
                            (width, height), style=style, accent=accent,
                            accent2=str(scene.get("accent2") or "#9A6CFF"),
                        ),
                        card,
                    )
                    clip = {
                        "src": str(card), "kind": "image", "duration": scene_duration,
                        "kenburns": {"zoom_from": 1.0, "zoom_to": 1.1, "direction": "center"},
                        "fade_out": 0.3,
                    }
                _add_transition(clip, i + 1)
                video_clips.append(clip)

                # Scene typography appears over the clip with its own timing.
                text_start = 0.0  # relative per-clip — recomputed below
                text_clips.append({
                    "text": kicker, "style": "kicker", "color": accent,
                    "position": "top", "start": _PLACEHOLDER, "duration": 1.6,
                    "fade_in": 0.3, "fade_out": 0.3,
                })
                text_clips.append({
                    "text": headline, "style": "title", "position": "center",
                    "start": _PLACEHOLDER, "duration": scene_duration - 0.6,
                    "fade_in": 0.45, "fade_out": 0.45,
                })
                if body:
                    text_clips.append({
                        "text": body, "style": "subtitle", "position": "bottom",
                        "start": _PLACEHOLDER, "duration": scene_duration - 1.0,
                        "fade_in": 0.5, "fade_out": 0.5,
                    })

            # Outro CTA card.
            outro_card = workdir / "card_outro.png"
            text_render.save_png(
                text_render.render_card(
                    cta, canvas_size=(width, height), style=style,
                    kicker=("مدعوم بجارفيس" if arabic else "POWERED BY JARVIS"),
                    accent=accent,
                ),
                outro_card,
            )
            outro_clip = {
                "src": str(outro_card), "kind": "image", "duration": 2.6,
                "kenburns": {"zoom_from": 1.1, "zoom_to": 1.0, "direction": "center"},
                "fade_in": 0.5, "fade_out": 0.8,
            }
            video_clips.append(outro_clip)

            # Resolve absolute text timings from the sequential video track.
            transition_total = 0.7
            cumulative = 0.0
            text_index = 0
            for clip_index, clip in enumerate(video_clips):
                duration = float(clip.get("duration") or scene_duration)
                scene_text_count = 3 if clip_index not in (0, len(video_clips) - 1) else 0
                for k in range(scene_text_count):
                    if text_index + k < len(text_clips):
                        text_clips[text_index + k]["start"] = round(cumulative + 0.5, 3)
                text_index += scene_text_count
                cumulative += duration
                if clip.get("transition_out"):
                    cumulative -= transition_total
            for tx in text_clips:
                if tx["start"] == _PLACEHOLDER:
                    tx["start"] = 0.5

            # Watermark.
            if logo:
                overlay_clips.append({
                    "src": str(logo), "position": "top-right", "scale": 0.1,
                    "opacity": 0.85, "start": 0, "duration": cumulative + 3,
                    "fade_in": 0.6, "fade_out": 0.6,
                })

            # Music bed.
            if music_path:
                audio_clips.append({
                    "src": music_path, "volume": music_volume,
                    "loop_to_fill": True, "fade_out": 1.8, "start": 0,
                })

            project = {
                "name": f"demo-{topic[:24]}",
                "width": width, "height": height, "fps": 30,
                "duck_original": 0.9,
                "tracks": [
                    {"type": "video", "clips": video_clips},
                    {"type": "overlay", "clips": overlay_clips},
                    {"type": "text", "clips": text_clips},
                    {"type": "audio", "clips": audio_clips},
                ],
                "export": {"format": "mp4", "quality": "high"},
            }
            result = render_timeline(project)
            size_mb = result["size_bytes"] / (1024 * 1024)
            content = (
                f"Demo video ✓ → `{result['path']}`\n"
                f"- {result['duration']}s @ {result['width']}x{result['height']},"
                f" {len(scenes) + 2} cards, {result['tracks']['text']} text layers\n"
                f"- Size: {size_mb:.2f} MB\n"
                f"{'- Music bed: ambient auto-synth\n' if music_path else ''}"
                f"▶ [watch the demo]({result['url']})"
                + (f"\nPoster: {result['thumbnail_url']}" if result.get("thumbnail_url") else "")
            )
            return ToolResult(tool_name="demo_video", content=content, success=True)
        except (FFmpegError, ValueError, OSError) as exc:
            logger.warning("demo_video failed: %s", exc)
            return ToolResult(tool_name="demo_video",
                              content=f"Demo composition failed: {exc}",
                              success=False)
        finally:
            import shutil

            shutil.rmtree(workdir, ignore_errors=True)


_PLACEHOLDER = -1.0


__all__ = ["DemoVideoTool"]
