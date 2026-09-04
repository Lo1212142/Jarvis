"""Creative image tools — generation + a full programmatic editing studio.

* ``media_image_generate`` — text-to-image via the configured provider
  (NVIDIA NIM by default, switchable from settings/UI/API).
* ``image_edit`` — a CapCut-grade image editor as a single tool: crop,
  zoom, filters, color grading, text/captions, watermarks, collages,
  format conversion … all Pillow/OpenCV, fully offline, Arabic-aware text.

Both return chat-renderable markdown (relative media URLs).
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

from openjarvis.creative import _paths, text_render
from openjarvis.creative import media_settings
from openjarvis.creative.providers import GenerationError, generate_image

logger = logging.getLogger(__name__)


def _md_image(url: str, label: str = "") -> str:
    alt = label or "image"
    return f"![{alt}]({url})"


def _load_input_image(src: str) -> Tuple[Path, "Any"]:
    """Load an image from a local path, creative-media path, or URL."""
    from PIL import Image

    if not src:
        raise ValueError("no image source provided")
    if src.startswith(("http://", "https://")):
        import httpx

        try:
            resp = httpx.get(src, follow_redirects=True, timeout=60.0)
            resp.raise_for_status()
        except Exception as exc:
            raise ValueError(f"failed to download image {src}: {exc}") from exc
        local = _paths.tmp_dir() / "downloaded-input.png"
        local.write_bytes(resp.content)
        return local, Image.open(io.BytesIO(resp.content)).convert("RGBA")
    local = Path(src).expanduser()
    if not local.is_absolute():
        for base in (Path.cwd(), _paths.creative_root()):
            candidate = base / local
            if candidate.exists():
                local = candidate
                break
    if not local.exists():
        raise ValueError(f"image not found: {src}")
    return local, Image.open(local).convert("RGBA")


def _save(img: "Any", *, stem: str, fmt: str = "png", quality: int = 92) -> Path:
    out = _paths.new_media_path("image", fmt, stem=stem)
    if fmt in ("jpg", "jpeg"):
        img.convert("RGB").save(out, quality=quality)
    elif fmt == "webp":
        img.convert("RGB").save(out, "WEBP", quality=quality)
    else:
        img.save(out, "PNG")
    return out


# ---------------------------------------------------------------------------
# media_image_generate tool
# ---------------------------------------------------------------------------


@ToolRegistry.register("media_image_generate")
class MediaImageGenerateTool(BaseTool):
    """Text-to-image generation through the flexible provider settings."""

    tool_id = "media_image_generate"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="media_image_generate",
            description=(
                "Generate images from a text prompt using the configured media"
                " provider (default: NVIDIA NIM flux.1-schnell; switchable via"
                " media settings). Saves to the media library and returns a"
                " chat-renderable markdown image."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed visual description of the image.",
                    },
                    "width": {"type": "integer", "default": 1024,
                              "description": "Image width in pixels (default 1024)."},
                    "height": {"type": "integer", "default": 1024,
                               "description": "Image height in pixels (default 1024)."},
                    "provider": {
                        "type": "string",
                        "description": "Provider override (default from settings).",
                    },
                    "model": {
                        "type": "string",
                        "description": "Model override (default from settings).",
                    },
                    "n": {"type": "integer", "default": 1,
                          "description": "Number of images (1-4)."},
                    "seed": {"type": "integer",
                             "description": "Optional deterministic seed."},
                },
                "required": ["prompt"],
            },
            category="media",
            timeout_seconds=300.0,
            required_capabilities=["network:fetch"],
        )

    def execute(self, **params: Any) -> ToolResult:
        prompt = str(params.get("prompt") or "").strip()
        if not prompt:
            return ToolResult(tool_name="media_image_generate",
                              content="No prompt provided.", success=False)
        try:
            width = int(params.get("width") or 1024)
            height = int(params.get("height") or 1024)
            result = generate_image(
                prompt,
                provider=params.get("provider"),
                model=params.get("model"),
                width=width,
                height=height,
                n=int(params.get("n") or 1),
                seed=params.get("seed"),
                stem="gen",
            )
        except (GenerationError, ValueError) as exc:
            logger.warning("media_image_generate failed: %s", exc)
            return ToolResult(tool_name="media_image_generate",
                              content=f"Image generation failed: {exc}",
                              success=False)
        lines = [f"Generated {len(result['images'])} image(s) via "
                 f"**{result['provider']}** (`{result['model']}`)."]
        for image in result["images"]:
            lines.append(_md_image(image["url"], prompt[:60]))
            lines.append(f"Saved: `{image['path']}`")
        return ToolResult(tool_name="media_image_generate",
                          content="\n".join(lines), success=True)


# ---------------------------------------------------------------------------
# image_edit tool — the studio
# ---------------------------------------------------------------------------

_OPS = (
    "crop resize rotate flip zoom sharpen blur brightness contrast saturation "
    "grayscale sepia invert vignette border rounded circle watermark text "
    "caption meme collage convert compress thumbnail pad pixelate posterize "
    "solarize emboss edges sketch denoise stylize duotone tint autocontrast "
    "equalize trim overlay style"
).split()


@ToolRegistry.register("image_edit")
class ImageEditTool(BaseTool):
    """Professional image editing studio (Pillow + OpenCV), fully offline."""

    tool_id = "image_edit"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="image_edit",
            description=(
                "Edit an image like a pro, entirely offline. Operations: "
                "crop, resize, rotate, flip, zoom, sharpen, blur, brightness,"
                " contrast, saturation, grayscale, sepia, invert, vignette,"
                " border, rounded corners, circle crop, watermark, text"
                " (full Arabic support), caption, meme, collage, convert"
                " (png/jpg/webp), compress, thumbnail, pad, pixelate,"
                " posterize, solarize, emboss, edges, sketch, denoise,"
                " stylize, duotone, tint, autocontrast, equalize, trim"
                " borders, overlay, and style presets (vintage, cinematic,"
                " noir, pastel, vivid, dreamy). Chain multiple operations by"
                " calling repeatedly — each call returns the edited file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "image": {
                        "type": "string",
                        "description": "Path (or URL) of the image to edit.",
                    },
                    "operation": {
                        "type": "string",
                        "enum": list(_OPS),
                        "description": "The editing operation to perform.",
                    },
                    "value": {
                        "type": "number",
                        "description": (
                            "Generic amount/parameter for the operation"
                            " (e.g. rotate degrees, blur sigma, brightness"
                            " 0.0-2.0, zoom factor)."
                        ),
                    },
                    "text": {
                        "type": "string",
                        "description": "Text for text/caption/meme/watermark ops.",
                    },
                    "x": {"type": "integer", "description": "X for crop (left) or overlay position."},
                    "y": {"type": "integer", "description": "Y for crop (top) or overlay position."},
                    "width": {"type": "integer", "description": "Width for crop/resize/pad/collage."},
                    "height": {"type": "integer", "description": "Height for crop/resize/pad/collage."},
                    "radius": {"type": "integer", "description": "Corner radius (rounded op)."},
                    "color": {"type": "string", "description": "Color (border/tint/text)."},
                    "color2": {"type": "string", "description": "Second color (duotone)."},
                    "position": {
                        "type": "string",
                        "description": "Position for watermark/overlay/text (center, top-left, bottom-right …).",
                    },
                    "opacity": {"type": "number", "description": "Opacity 0-1 (overlay/watermark)."},
                    "scale": {"type": "number", "description": "Scale factor (watermark/overlay)."},
                    "format": {
                        "type": "string", "enum": ["png", "jpg", "webp"],
                        "description": "Output format (default keeps original).",
                    },
                    "quality": {"type": "integer", "description": "JPEG/WEBP quality 10-100."},
                    "images": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Image list for collage/overlay-second.",
                    },
                    "style": {
                        "type": "string",
                        "enum": ["vintage", "cinematic", "noir", "pastel",
                                 "vivid", "dreamy", "cyberpunk"],
                        "description": "Style preset name.",
                    },
                },
                "required": ["image", "operation"],
            },
            category="media",
            timeout_seconds=120.0,
        )

    # -- operation implementations ---------------------------------------

    @staticmethod
    def _op_crop(img: Any, p: Dict[str, Any]) -> Any:
        x, y = int(p.get("x") or 0), int(p.get("y") or 0)
        w = int(p.get("width") or 0)
        h = int(p.get("height") or 0)
        if w <= 0 or h <= 0:
            raise ValueError("crop needs positive width/height")
        if x < 0 or y < 0 or x + w > img.width or y + h > img.height:
            raise ValueError(
                f"crop {x}+{w}/{y}+{h} exceeds image {img.width}x{img.height}"
            )
        return img.crop((x, y, x + w, y + h))

    @staticmethod
    def _op_resize(img: Any, p: Dict[str, Any]) -> Any:
        w = int(p.get("width") or 0)
        h = int(p.get("height") or 0)
        if w <= 0 or h <= 0:
            raise ValueError("resize needs positive width/height")
        return img.resize((w, h), Image.LANCZOS)  # type: ignore[attr-defined]

    @staticmethod
    def _op_rotate(img: Any, p: Dict[str, Any]) -> Any:
        angle = float(p.get("value") or 0)
        return img.rotate(angle, expand=True, resample=Image.BICUBIC,
                          fillcolor=(0, 0, 0, 0))

    @staticmethod
    def _op_flip(img: Any, p: Dict[str, Any]) -> Any:
        axis = str(p.get("axis") or str(p.get("value") or "horizontal")).lower()
        return img.transpose(Image.FLIP_TOP_BOTTOM if axis.startswith("v") else Image.FLIP_LEFT_RIGHT)

    @staticmethod
    def _op_zoom(img: Any, p: Dict[str, Any]) -> Any:
        factor = float(p.get("value") or 1.25)
        if factor < 1.0:
            raise ValueError("zoom factor must be >= 1.0 (use resize to shrink)")
        w, h = img.size
        cw, ch = int(w / factor), int(h / factor)
        x, y = (w - cw) // 2, (h - ch) // 2
        return img.crop((x, y, x + cw, y + ch)).resize((w, h), Image.LANCZOS)  # type: ignore[attr-defined]

    @staticmethod
    def _op_sharpen(img: Any, p: Dict[str, Any]) -> Any:
        from PIL import ImageFilter

        amount = float(p.get("value") or 2.0)
        return img.filter(ImageFilter.UnsharpMask(radius=2, percent=int(amount * 50)))

    @staticmethod
    def _op_blur(img: Any, p: Dict[str, Any]) -> Any:
        from PIL import ImageFilter

        radius = float(p.get("value") or 3.0)
        return img.filter(ImageFilter.GaussianBlur(radius=radius))

    @staticmethod
    def _op_brightness(img: Any, p: Dict[str, Any]) -> Any:
        from PIL import ImageEnhance

        return ImageEnhance.Brightness(img).enhance(float(p.get("value") or 1.2))

    @staticmethod
    def _op_contrast(img: Any, p: Dict[str, Any]) -> Any:
        from PIL import ImageEnhance

        return ImageEnhance.Contrast(img).enhance(float(p.get("value") or 1.2))

    @staticmethod
    def _op_saturation(img: Any, p: Dict[str, Any]) -> Any:
        from PIL import ImageEnhance

        return ImageEnhance.Color(img).enhance(float(p.get("value") or 1.3))

    @staticmethod
    def _op_grayscale(img: Any, p: Dict[str, Any]) -> Any:
        return img.convert("L").convert("RGBA")

    @staticmethod
    def _op_sepia(img: Any, p: Dict[str, Any]) -> Any:
        gray = img.convert("L")
        r = gray.point(lambda v: min(255, int(v * 1.04)))
        g = gray.point(lambda v: min(255, int(v * 0.85)))
        b = gray.point(lambda v: min(255, int(v * 0.62)))
        return Image.merge("RGB", (r, g, b)).convert("RGBA")

    @staticmethod
    def _op_invert(img: Any, p: Dict[str, Any]) -> Any:
        from PIL import ImageOps

        return ImageOps.invert(img.convert("RGB")).convert("RGBA")

    @staticmethod
    def _op_vignette(img: Any, p: Dict[str, Any]) -> Any:
        import numpy as np

        strength = float(p.get("value") or 1.0)
        w, h = img.size
        y, x = np.ogrid[:h, :w]
        cx, cy = w / 2, h / 2
        dist = np.sqrt(((x - cx) / (w / 2)) ** 2 + ((y - cy) / (h / 2)) ** 2)
        mask = np.clip(1.15 - dist * strength, 0, 1)
        arr = np.asarray(img.convert("RGB")).astype(np.float32)
        arr *= mask[..., None]
        return Image.fromarray(arr.clip(0, 255).astype("uint8")).convert("RGBA")

    @staticmethod
    def _op_border(img: Any, p: Dict[str, Any]) -> Any:
        from PIL import ImageOps

        width_px = int(p.get("value") or 12)
        color = str(p.get("color") or "#FFFFFF")
        return ImageOps.expand(img, border=width_px, fill=tuple(int(color[i:i + 2], 16) for i in (1, 3, 5)))

    @staticmethod
    def _op_rounded(img: Any, p: Dict[str, Any]) -> Any:
        radius = int(p.get("radius") or 40)
        from PIL import ImageDraw

        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, *img.size], radius=radius, fill=255)
        out = Image.new("RGBA", img.size, (0, 0, 0, 0))
        out.paste(img, (0, 0), mask)
        return out

    @staticmethod
    def _op_circle(img: Any, p: Dict[str, Any]) -> Any:
        from PIL import ImageDraw

        w, h = img.size
        side = min(w, h)
        img = img.crop(((w - side) // 2, (h - side) // 2,
                        (w - side) // 2 + side, (h - side) // 2 + side))
        mask = Image.new("L", (side, side), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, side, side], fill=255)
        out = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        out.paste(img, (0, 0), mask)
        return out

    @staticmethod
    def _apply_watermark(img: Any, p: Dict[str, Any]) -> Any:
        wm_src = str(p.get("images") or p.get("watermark") or "").strip()
        text = str(p.get("text") or "").strip()
        position = str(p.get("position") or "bottom-right")
        scale = float(p.get("scale") or 0.18)
        opacity = float(p.get("opacity") or 0.85)
        margin = int(p.get("margin") or 24)
        W, H = img.size
        if wm_src:
            _, wm = _load_input_image(wm_src if wm_src else "")
            wm = wm.resize((max(1, int(W * scale)), max(1, int(W * scale * wm.height / wm.width))), Image.LANCZOS)  # type: ignore[attr-defined]
        else:
            wm = text_render.render_text_overlay(
                text or "© Jarvis",
                canvas_size=(W, H), style="watermark",
                position=position, padding=margin,
            )
            bbox = wm.getbbox()
            wm = wm.crop(bbox) if bbox else wm
        if opacity < 1:
            alpha = wm.getchannel("A").point(lambda a: int(a * opacity))
            wm.putalpha(alpha)
        pos_map = {
            "top-left": (margin, margin),
            "top-right": (W - wm.width - margin, margin),
            "bottom-left": (margin, H - wm.height - margin),
            "bottom-right": (W - wm.width - margin, H - wm.height - margin),
            "center": ((W - wm.width) // 2, (H - wm.height) // 2),
        }
        xy = pos_map.get(position, pos_map["bottom-right"])
        base = img.copy()
        base.alpha_composite(wm, xy)
        return base

    @staticmethod
    def _op_text(img: Any, p: Dict[str, Any]) -> Any:
        message = str(p.get("text") or "")
        if not message:
            raise ValueError("text op requires 'text'")
        overlay = text_render.render_text_overlay(
            message,
            canvas_size=img.size,
            style=str(p.get("style") or "caption"),
            color=p.get("color"),
            position=str(p.get("position") or "bottom"),
            font_path=p.get("font_path"),
        )
        out = img.copy()
        out.alpha_composite(overlay)
        return out

    @staticmethod
    def _op_caption(img: Any, p: Dict[str, Any]) -> Any:
        """Bottom caption with a translucent band (meme-safe, Arabic-aware)."""
        from PIL import Image, ImageDraw

        message = str(p.get("text") or "")
        if not message:
            raise ValueError("caption op requires 'text'")
        W, H = img.size
        band_h = int(H * 0.16)
        band = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(band)
        draw.rectangle([0, H - band_h, W, H], fill=(0, 0, 0, 150))
        overlay = text_render.render_text_overlay(
            message, canvas_size=(W, H), style="caption",
            position=f"bottom+{int(band_h * 0.18)}", color="#FFFFFF",
        )
        band.alpha_composite(overlay)
        out = img.copy()
        out.alpha_composite(band)
        return out

    @staticmethod
    def _op_meme(img: Any, p: Dict[str, Any]) -> Any:
        message = str(p.get("text") or "")
        if not message:
            raise ValueError("meme op requires 'text' (top+bottom separated by |)")
        W, H = img.size
        top_text, _, bottom_text = message.partition("|")
        out = img.copy()
        if top_text.strip():
            out.alpha_composite(text_render.render_text_overlay(
                top_text.strip(), canvas_size=(W, H), style="title",
                position="top", padding=int(H * 0.04)))
        if bottom_text.strip():
            out.alpha_composite(text_render.render_text_overlay(
                bottom_text.strip(), canvas_size=(W, H), style="title",
                position="bottom", padding=int(H * 0.04)))
        return out

    @staticmethod
    def _op_collage(img: Any, p: Dict[str, Any]) -> Any:
        sources: List[str] = list(p.get("images") or [])
        if len(sources) < 1:
            raise ValueError("collage needs 'images' list (plus the main image)")
        paths = [p.get("__main_src__", "")] + sources
        images = []
        for src in paths:
            if src:
                _, loaded = _load_input_image(str(src))
                images.append(loaded.convert("RGB"))
        if len(images) < 2:
            raise ValueError("collage needs at least two images")
        cols = int(p.get("width") or min(len(images), 3))
        rows = (len(images) + cols - 1) // cols
        cell_w = max(img.width // cols, 64)
        cell_h = max(img.height // rows, 64)
        canvas = Image.new("RGB", (cols * cell_w, rows * cell_h), (12, 12, 20))
        for i, image in enumerate(images):
            fitted = image.resize((cell_w, cell_h), Image.LANCZOS)  # type: ignore[attr-defined]
            canvas.paste(fitted, ((i % cols) * cell_w, (i // cols) * cell_h))
        return canvas.convert("RGBA")

    @staticmethod
    def _op_pad(img: Any, p: Dict[str, Any]) -> Any:
        w = int(p.get("width") or img.width)
        h = int(p.get("height") or img.height)
        color = str(p.get("color") or "#000000")
        from PIL import Image

        canvas = Image.new("RGBA", (w, h), tuple(int(color[i:i + 2], 16) for i in (1, 3, 5)) + (255,) if color.startswith("#") else (0, 0, 0, 255))
        canvas.alpha_composite(img, ((w - img.width) // 2, (h - img.height) // 2))
        return canvas

    @staticmethod
    def _op_pixelate(img: Any, p: Dict[str, Any]) -> Any:
        blocks = max(2, int(p.get("value") or 16))
        small = img.resize(
            (max(1, img.width // blocks), max(1, img.height // blocks)),
            Image.NEAREST,
        )
        return small.resize(img.size, Image.NEAREST)

    @staticmethod
    def _op_posterize(img: Any, p: Dict[str, Any]) -> Any:
        from PIL import ImageOps

        bits = max(1, min(8, int(p.get("value") or 4)))
        return ImageOps.posterize(img.convert("RGB"), bits).convert("RGBA")

    @staticmethod
    def _op_solarize(img: Any, p: Dict[str, Any]) -> Any:
        from PIL import ImageOps

        threshold = int(p.get("value") or 128)
        return ImageOps.solarize(img.convert("RGB"), threshold=threshold).convert("RGBA")

    @staticmethod
    def _op_emboss(img: Any, p: Dict[str, Any]) -> Any:
        from PIL import ImageFilter

        return img.filter(ImageFilter.EMBOSS)

    @staticmethod
    def _op_edges(img: Any, p: Dict[str, Any]) -> Any:
        import cv2
        import numpy as np

        arr = np.asarray(img.convert("RGB"))
        edges = cv2.Canny(cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY), 80, 200)
        return Image.fromarray(edges).convert("RGBA")

    @staticmethod
    def _op_sketch(img: Any, p: Dict[str, Any]) -> Any:
        import cv2
        import numpy as np

        arr = np.asarray(img.convert("RGB"))
        gray, _ = cv2.pencilSketch(
            arr, sigma_s=60, sigma_r=0.07, shade_factor=0.05
        ) if hasattr(cv2, "pencilSketch") else (cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY), None)
        return Image.fromarray(gray).convert("RGBA")

    @staticmethod
    def _op_denoise(img: Any, p: Dict[str, Any]) -> Any:
        import cv2
        import numpy as np

        arr = np.asarray(img.convert("RGB"))
        den = cv2.fastNlMeansDenoisingColored(arr, None, 6, 6, 7, 21)
        return Image.fromarray(den).convert("RGBA")

    @staticmethod
    def _op_stylize(img: Any, p: Dict[str, Any]) -> Any:
        import cv2
        import numpy as np

        arr = np.asarray(img.convert("RGB"))
        styl = cv2.stylization(arr, sigma_s=60, sigma_r=0.45)
        return Image.fromarray(styl).convert("RGBA")

    @staticmethod
    def _op_duotone(img: Any, p: Dict[str, Any]) -> Any:
        import numpy as np

        color1 = str(p.get("color") or "#1B2A6B")
        color2 = str(p.get("color2") or "#F5C518")
        c1 = tuple(int(color1[i:i + 2], 16) for i in (1, 3, 5))
        c2 = tuple(int(color2[i:i + 2], 16) for i in (1, 3, 5))
        gray = np.asarray(img.convert("L")).astype(np.float32) / 255.0
        arr = np.zeros((*gray.shape, 3), dtype=np.uint8)
        for ch in range(3):
            arr[..., ch] = (c1[ch] + (c2[ch] - c1[ch]) * gray).astype(np.uint8)
        return Image.fromarray(arr).convert("RGBA")

    @staticmethod
    def _op_tint(img: Any, p: Dict[str, Any]) -> Any:
        import numpy as np

        color = str(p.get("color") or "#FF8844")
        rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
        strength = float(p.get("value") or 0.35)
        arr = np.asarray(img.convert("RGB")).astype(np.float32)
        tint = np.array(rgb, dtype=np.float32)
        arr = arr * (1 - strength) + tint * strength
        return Image.fromarray(arr.clip(0, 255).astype("uint8")).convert("RGBA")

    @staticmethod
    def _op_autocontrast(img: Any, p: Dict[str, Any]) -> Any:
        from PIL import ImageOps

        return ImageOps.autocontrast(img.convert("RGB")).convert("RGBA")

    @staticmethod
    def _op_equalize(img: Any, p: Dict[str, Any]) -> Any:
        from PIL import ImageOps

        return ImageOps.equalize(img.convert("RGB")).convert("RGBA")

    @staticmethod
    def _op_trim(img: Any, p: Dict[str, Any]) -> Any:
        from PIL import ImageChops

        bg = Image.new("RGBA", img.size, (0, 0, 0, 0))
        diff = ImageChops.difference(img, bg)
        bbox = diff.getbbox()
        return img.crop(bbox) if bbox else img

    @staticmethod
    def _op_overlay(img: Any, p: Dict[str, Any]) -> Any:
        sources: List[str] = list(p.get("images") or [])
        if not sources:
            raise ValueError("overlay needs 'images' (the image on top)")
        top_src = sources[0]
        _, top = _load_input_image(top_src)
        scale = float(p.get("scale") or 0.4)
        opacity = float(p.get("opacity") or 1.0)
        position = str(p.get("position") or "center")
        W, H = img.size
        top = top.resize((int(W * scale), int(W * scale * top.height / top.width)),
                         Image.LANCZOS)  # type: ignore[attr-defined]
        if opacity < 1:
            alpha = top.getchannel("A").point(lambda a: int(a * opacity))
            top.putalpha(alpha)
        pos_map = {
            "top-left": (0, 0), "top-right": (W - top.width, 0),
            "bottom-left": (0, H - top.height), "bottom-right": (W - top.width, H - top.height),
            "center": ((W - top.width) // 2, (H - top.height) // 2),
        }
        base = img.copy()
        base.alpha_composite(top, pos_map.get(position, pos_map["center"]))
        return base

    @staticmethod
    def _op_convert(img: Any, p: Dict[str, Any]) -> Any:
        """Format conversion / re-encode (the format is applied at save)."""
        return img

    @staticmethod
    def _op_compress(img: Any, p: Dict[str, Any]) -> Any:
        """Compress — quality is applied at save time (default 70)."""
        p.setdefault("quality", max(10, min(100, int(p.get("quality") or 70))))
        return img

    @staticmethod
    def _op_thumbnail(img: Any, p: Dict[str, Any]) -> Any:
        """Aspect-preserving resize to a target width (default 320)."""
        target_w = max(16, int(p.get("width") or 320))
        ratio = target_w / img.width
        return img.resize((target_w, max(16, int(img.height * ratio))),
                          Image.LANCZOS)

    @staticmethod
    def _op_style(img: Any, p: Dict[str, Any]) -> Any:
        style = str(p.get("style") or str(p.get("value") or "cinematic")).lower()
        from PIL import ImageEnhance, ImageOps

        presets = {
            "vintage": [("Color", 0.75), ("Contrast", 1.05), ("Brightness", 1.05)],
            "cinematic": [("Color", 1.15), ("Contrast", 1.25), ("Brightness", 0.95)],
            "noir": [("Color", 0.0), ("Contrast", 1.4)],
            "pastel": [("Color", 0.85), ("Brightness", 1.12), ("Contrast", 0.9)],
            "vivid": [("Color", 1.45), ("Contrast", 1.15)],
            "dreamy": [("Color", 0.95), ("Brightness", 1.08), ("Contrast", 0.92)],
            "cyberpunk": [("Color", 1.5), ("Contrast", 1.3)],
        }
        steps = presets.get(style, presets["cinematic"])
        out = img
        for attr, factor in steps:
            if attr == "Color":
                out = ImageEnhance.Color(out).enhance(factor)
            elif attr == "Contrast":
                out = ImageEnhance.Contrast(out).enhance(factor)
            elif attr == "Brightness":
                out = ImageEnhance.Brightness(out).enhance(factor)
        if style == "vintage":
            out = ImageEditTool._op_sepia(out, {})
            out = ImageEnhance.Color(out).enhance(1.25)
        if style == "cyberpunk":
            out = ImageEditTool._op_tint(out, {"color": "#2A6BFF", "value": 0.22})
        return out.convert("RGBA")

    def execute(self, **params: Any) -> ToolResult:
        src = str(params.get("image") or "").strip()
        operation = str(params.get("operation") or "").strip().lower()
        if not src:
            return ToolResult(tool_name="image_edit", content="No image provided.", success=False)
        if operation not in _OPS:
            return ToolResult(
                tool_name="image_edit",
                content=f"Unknown operation '{operation}'. Valid: {', '.join(_OPS)}",
                success=False,
            )
        try:
            local_path, img = _load_input_image(src)
        except ValueError as exc:
            return ToolResult(tool_name="image_edit", content=str(exc), success=False)

        dispatch = {
            "crop": self._op_crop, "resize": self._op_resize,
            "rotate": self._op_rotate, "flip": self._op_flip,
            "zoom": self._op_zoom, "sharpen": self._op_sharpen,
            "blur": self._op_blur, "brightness": self._op_brightness,
            "contrast": self._op_contrast, "saturation": self._op_saturation,
            "grayscale": self._op_grayscale, "sepia": self._op_sepia,
            "invert": self._op_invert, "vignette": self._op_vignette,
            "border": self._op_border, "rounded": self._op_rounded,
            "circle": self._op_circle, "watermark": self._apply_watermark,
            "text": self._op_text, "caption": self._op_caption,
            "meme": self._op_meme, "collage": self._op_collage,
            "pad": self._op_pad, "pixelate": self._op_pixelate,
            "posterize": self._op_posterize, "solarize": self._op_solarize,
            "emboss": self._op_emboss, "edges": self._op_edges,
            "sketch": self._op_sketch, "denoise": self._op_denoise,
            "stylize": self._op_stylize, "duotone": self._op_duotone,
            "tint": self._op_tint, "autocontrast": self._op_autocontrast,
            "equalize": self._op_equalize, "trim": self._op_trim,
            "overlay": self._op_overlay, "style": self._op_style,
            "convert": self._op_convert, "compress": self._op_compress,
            "thumbnail": self._op_thumbnail,
        }
        handler = dispatch.get(operation)
        if handler is None:
            return ToolResult(tool_name="image_edit",
                              content=f"Operation '{operation}' not implemented yet.",
                              success=False)
        try:
            params["__main_src__"] = str(local_path)
            edited = handler(img, params)
        except (ValueError, OSError) as exc:
            return ToolResult(tool_name="image_edit",
                              content=f"Edit failed: {exc}", success=False)
        fmt = str(params.get("format") or "").lower()
        if fmt not in ("png", "jpg", "webp"):
            fmt = "png" if operation in ("rounded", "circle", "text", "watermark", "overlay", "meme", "caption") else "png"
        try:
            quality = int(params.get("quality") or 92)
            out = _save(edited, stem=f"{local_path.stem[:32]}-{operation}", fmt=fmt, quality=quality)
        except (OSError, ValueError) as exc:
            return ToolResult(tool_name="image_edit",
                              content=f"Save failed: {exc}", success=False)
        size_kb = out.stat().st_size // 1024
        content = (
            f"{operation} ✓  → `{out.name}` ({edited.width}x{edited.height}, {size_kb} KB)\n"
            + _md_image(_paths.media_url(out), operation)
        )
        return ToolResult(tool_name="image_edit", content=content, success=True)


__all__ = ["MediaImageGenerateTool", "ImageEditTool", "_load_input_image"]
