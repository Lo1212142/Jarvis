"""Remote audio playback coordination primitives."""

from .playback import AudioPlaybackService, PlaybackState, StreamGrant, get_default_audio_service

__all__ = ["AudioPlaybackService", "PlaybackState", "StreamGrant", "get_default_audio_service"]
