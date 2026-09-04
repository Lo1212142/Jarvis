"""Professional text rendering for the creative studio (images + videos).

Renders text overlays as transparent PNGs with Pillow. Pillow is compiled
with libraqm on most modern installs, which gives correct Arabic shaping and
bidirectional text; when raqm is unavailable we fall back to
``arabic_reshaper`` + ``python-bidi`` pre-shaping, and finally to plain
rendering (English-only).

The renderer powers:
* video ``text`` clips (titles, captions, lower-thirds …)
* image captions / watermarks
* demo-video title & scene cards

Style presets intentionally follow the "big AI company demo" aesthetic:
huge bold type, generous spacing, soft shadows, thin accent bars.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")

# Preferred bold/regular font files, first match wins (cross-platform).
_FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/noto-sans-sc/NotoSansSC-Bold.otf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]
_FONT_CANDIDATES_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]

_RQM = False
try:  # Pillow ≥ 8 with libraqm
    _RQM = bool(ImageFont.core.HB if hasattr(ImageFont, "core") else False)
except Exception:  # pragma: no cover
    _RQM = False
try:
    from PIL import features as _pil_features

    _RQM = bool(_pil_features.check("raqm"))
except Exception:  # pragma: no cover
    pass


def has_arabic(text: str) -> bool:
    return bool(_ARABIC_RE.search(text or ""))


def shape_text(text: str) -> str:
    """Return display-ready text (Arabic shaped + bidi when raqm missing)."""
    if not text:
        return text
    if _RQM:
        return text  # raqm handles shaping+bidi at draw time
    if has_arabic(text):
        try:
            import arabic_reshaper
            from bidi.algorithm import get_display

            reshaped = arabic_reshaper.reshape(text)
            return get_display(reshaped)
        except Exception:
            return text
    return text


def find_font(bold: bool = True, extra_dirs: Optional[List[str]] = None) -> Optional[str]:
    """Locate a usable TTF for headline rendering."""
    candidates = list(_FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_REGULAR)
    for directory in extra_dirs or []:
        if directory and os.path.isdir(directory):
            for name in sorted(os.listdir(directory)):
                if name.lower().endswith((".ttf", ".otf")):
                    candidates.append(os.path.join(directory, name))
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def _load_font(size: int, bold: bool = True, font_path: Optional[str] = None) -> ImageFont.FreeTypeFont:
    if font_path and os.path.exists(font_path):
        return ImageFont.truetype(font_path, size)
    found = find_font(bold=bold)
    if found:
        return ImageFont.truetype(found, size)
    try:
        return ImageFont.load_default(size)
    except TypeError:  # older Pillow without size arg
        return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> List[str]:
    """Greedy word-wrap honoring explicit newlines; Arabic-safe (no joining)."""
    lines: List[str] = []
    for paragraph in (text or "").split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        words = paragraph.split(" ")
        current = ""
        for word in words:
            trial = f"{current} {word}".strip()
            if draw.textlength(trial, font=font) <= max_width or not current:
                current = trial
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
    return lines


def _auto_fit(
    image: Image.Image,
    text: str,
    *,
    max_width_px: int,
    target_height_ratio: float,
    bold: bool = True,
    min_size: int = 18,
    max_size: int = 220,
    font_path: Optional[str] = None,
) -> Tuple[ImageFont.FreeTypeFont, List[str]]:
    """Find the largest font size whose wrapped text fits the target box."""
    draw = ImageDraw.Draw(image)
    best: Tuple[Optional[ImageFont.FreeTypeFont], List[str], int] = (None, [], min_size)
    size = max(min_size, int(max_size * 0.5))
    low, high = min_size, max_size
    while low <= high:
        mid = (low + high) // 2
        font = _load_font(mid, bold=bold, font_path=font_path)
        lines = _wrap(draw, shape_text(text), font, max_width_px)
        line_h = mid * 1.32
        total_h = line_h * len(lines)
        widest = max((draw.textlength(line, font=font) for line in lines), default=0)
        if widest <= max_width_px and total_h <= image.height * target_height_ratio:
            best = (font, lines, mid)
            low = mid + 1
        else:
            high = mid - 1
    if best[0] is None:
        font = _load_font(min_size, bold=bold, font_path=font_path)
        return font, _wrap(draw, shape_text(text), font, max_width_px)
    return best[0], best[1]


_STYLE_PRESETS: Dict[str, Dict[str, float | str]] = {
    # name: {size_ratio (of canvas height), color, shadow, letter_spacing}
    "title": {"size_ratio": 0.12, "color": "#FFFFFF", "shadow": 1, "bold": 1},
    "subtitle": {"size_ratio": 0.062, "color": "#E6E9F2", "shadow": 1, "bold": 0},
    "caption": {"size_ratio": 0.045, "color": "#FFFFFF", "shadow": 1, "bold": 0},
    "kicker": {"size_ratio": 0.032, "color": "#8AB4FF", "shadow": 0, "bold": 1},
    "quote": {"size_ratio": 0.07, "color": "#F5F0E1", "shadow": 1, "bold": 0},
    "lower-third": {"size_ratio": 0.05, "color": "#FFFFFF", "shadow": 1, "bold": 1},
    "watermark": {"size_ratio": 0.03, "color": "#FFFFFF", "shadow": 1, "bold": 1},
}


def render_text_overlay(
    text: str,
    *,
    canvas_size: Tuple[int, int],
    style: str = "title",
    color: Optional[str] = None,
    font_path: Optional[str] = None,
    align: str = "center",
    position: str = "center",
    padding: int = 64,
    bold: Optional[bool] = None,
    max_lines: Optional[int] = None,
    outline: bool = False,
) -> Image.Image:
    """Render *text* onto a transparent RGBA canvas of exact *canvas_size*.

    The text block is placed according to ``position``
    (center/top/bottom/top-left/bottom-left/…). A soft shadow is drawn for
    readability over any background.
    """
    W, H = canvas_size
    preset = _STYLE_PRESETS.get(style, _STYLE_PRESETS["title"])
    base_size = int(H * float(preset["size_ratio"]))
    is_bold = bool(preset["bold"]) if bold is None else bold
    font, lines = _auto_fit(
        Image.new("RGB", (8, 8)),
        text,
        max_width_px=W - 2 * padding,
        target_height_ratio=0.9,
        bold=is_bold,
        min_size=max(14, base_size // 2),
        max_size=base_size,
        font_path=font_path,
    )
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
    text_color = color or str(preset["color"])

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    line_h = font.size * 1.32
    block_h = line_h * len(lines)

    # Vertical placement — supports "center", "top", "bottom", "top-left" …
    # optionally with a pixel offset like "top+120" or "center-40".
    m = re.match(r"^\s*([a-zA-Z-]*?)\s*(?:([+-])(\d+))?\s*$", position or "center")
    base = (m.group(1) if m and m.group(1) else "center").lower()
    dy = 0
    if m and m.group(2) and m.group(3):
        dy = int(m.group(3)) * (-1 if m.group(2) == "-" else 1)
    if base.startswith("top"):
        y = padding + dy
    elif base.startswith("bottom"):
        y = H - block_h - padding + dy
    else:  # center / middle / unknown
        y = (H - block_h) / 2 + dy

    # Horizontal alignment
    pos_horizontal = base if base not in ("top", "bottom", "center", "middle") else ""
    def x_for(line: str) -> float:
        w = draw.textlength(line, font=font)
        effective_align = align if align != "auto" else "center"
        if effective_align == "center" and not pos_horizontal:
            return (W - w) / 2
        if effective_align == "right" or pos_horizontal.endswith("right"):
            return W - w - padding
        if effective_align == "left" or pos_horizontal.endswith("left"):
            return padding
        return (W - w) / 2

    # Shadow / outline pass

    if int(preset.get("shadow", 0)) or outline:
        shadow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sdraw = ImageDraw.Draw(shadow_layer)
        for i, line in enumerate(lines):
            xy = (x_for(line) + 3, y + i * line_h + 3)
            sdraw.text(xy, line, font=font, fill=(0, 0, 0, 190))
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=font.size / 14))
        img.alpha_composite(shadow_layer)

    for i, line in enumerate(lines):
        xy = (x_for(line), y + i * line_h)
        draw.text(xy, line, font=font, fill=text_color)

    return img


def render_gradient_background(
    size: Tuple[int, int],
    *,
    style: str = "dark",
    accent: str = "#6C8CFF",
    accent2: str = "#9A6CFF",
) -> Image.Image:
    """Studio gradient background (dark tech / light minimal / brand)."""
    W, H = size
    img = Image.new("RGB", (W, H), (0, 0, 0))
    top, bottom = _GRADIENTS.get(style, _GRADIENTS["dark"])
    for y in range(H):
        t = y / max(1, H - 1)
        row = tuple(int(top[c] + (bottom[c] - top[c]) * t) for c in range(3))
        # fill row via numpy-free fast path
        ImageDraw.Draw(img).line([(0, y), (W, y)], fill=row)
    # Soft radial accent glow (two blurred ellipses).
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    for (cx, cy, r, col) in (
        (int(W * 0.78), int(H * 0.22), int(W * 0.35), accent),
        (int(W * 0.2), int(H * 0.85), int(W * 0.4), accent2),
    ):
        odraw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col + "44")
    overlay = overlay.filter(ImageFilter.GaussianBlur(radius=int(W * 0.09)))
    img = img.convert("RGBA")
    img.alpha_composite(overlay)
    return img.convert("RGB")


_GRADIENTS: Dict[str, Tuple[Tuple[int, int, int], Tuple[int, int, int]]] = {
    "dark": ((10, 12, 24), (26, 30, 52)),
    "darker": ((5, 6, 12), (14, 16, 30)),
    "light": ((246, 248, 252), (222, 228, 240)),
    "warm": ((30, 20, 16), (58, 36, 26)),
    "ocean": ((8, 20, 38), (16, 52, 84)),
}


def render_card(
    headline: str,
    *,
    canvas_size: Tuple[int, int],
    background: Optional[Image.Image] = None,
    style: str = "dark",
    subtitle: str = "",
    kicker: str = "",
    accent: str = "#6C8CFF",
    headline_color: Optional[str] = None,
    font_path: Optional[str] = None,
    headline_ratio: float = 0.13,
) -> Image.Image:
    """Compose a full-bleed title/scene card (background + kicker + headline + subtitle)."""
    W, H = canvas_size
    base = background if background is not None else render_gradient_background((W, H), style=style, accent=accent)
    base = base.convert("RGBA").resize((W, H), Image.LANCZOS)

    if kicker:
        kick = render_text_overlay(
            kicker.upper(),
            canvas_size=(W, H),
            style="kicker",
            color=accent,
            position="top",
            align="center",
            font_path=font_path,
            padding=int(H * 0.14),
        )
        base.alpha_composite(kick)

    headline_img = render_text_overlay(
        headline,
        canvas_size=(W, H),
        style="title",
        color=headline_color,
        position="center",
        align="center",
        font_path=font_path,
        padding=int(W * 0.08),
    )
    base.alpha_composite(headline_img)

    if subtitle:
        # Below the centered headline: lower-third placement reads cleanly
        # for both short and long headlines (no overlap by construction).
        sub_img = render_text_overlay(
            subtitle,
            canvas_size=(W, H),
            style="subtitle",
            position="bottom",
            align="center",
            font_path=font_path,
            padding=int(H * 0.16),
        )
        base.alpha_composite(sub_img)

    return base


def save_png(img: Image.Image, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG")
    return path


__all__ = [
    "has_arabic",
    "shape_text",
    "find_font",
    "render_text_overlay",
    "render_gradient_background",
    "render_card",
    "save_png",
]
