"""Language-agnostic wake-word matching for the Windows voice client."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


_ARABIC_DIACRITICS = re.compile(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]")


@dataclass(frozen=True, slots=True)
class WakeWordConfig:
    enabled: bool = True
    words: tuple[str, ...] = ("jarvis", "يا جارفيس")
    language: str = "multi"
    sensitivity: float = 0.75
    hot_window_seconds: int = 8

    def __post_init__(self) -> None:
        if self.language not in {"auto", "ar", "en", "multi"}:
            raise ValueError("language must be auto, ar, en, or multi")
        if not 0 <= self.sensitivity <= 1:
            raise ValueError("sensitivity must be between 0 and 1")
        if not 1 <= self.hot_window_seconds <= 60:
            raise ValueError("hot_window_seconds must be between 1 and 60")
        if len(self.words) > 10:
            raise ValueError("at most 10 wake words are supported")


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = _ARABIC_DIACRITICS.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def detect_wake_word(transcript: str, config: WakeWordConfig) -> str | None:
    """Return the matched configured word, or None.

    The client should call this on a short local transcript window. It should
    not upload passive microphone audio before this function returns a match.
    """
    if not config.enabled:
        return None
    normalized = normalize_text(transcript)
    for word in config.words:
        candidate = normalize_text(word)
        if candidate and re.search(rf"(?<!\w){re.escape(candidate)}(?!\w)", normalized):
            return word
    return None


__all__ = ["WakeWordConfig", "detect_wake_word", "normalize_text"]
